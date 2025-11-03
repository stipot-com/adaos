import express from 'express'
import pino from 'pino'
import { toInputEvent } from './normalize.js'
import { onTelegramUpdate, initTgRouting } from './router.js'
import { getFilePath, downloadFile, convertOpusToWav16k } from './media.js'
import { resolveHubId } from '../router/resolve.js'
import { idemGet, idemPut } from '../idem/kv.js'
import { extractStartCode } from './pairing.js'
import { pairConfirm, tgLinkSet } from '../pairing/store.js'
import { ensureSchema } from '../../db/tg.repo.js'
import { upsertBinding, listBindings, setSession } from '../../db/tg.repo.js'
import { NatsBus } from '../bus/nats.js'
import { randomUUID } from 'crypto'
import { tg_updates_total, enqueue_total, dlq_total } from '../telemetry.js'
// Local pairing only: resolve hub_id by locally issued code (pairCreate/pairConfirm)

const log = pino({ name: 'tg-webhook' })

export function installTelegramWebhookRoutes(app: express.Express, bus: NatsBus | null) {
	app.post('/io/tg/:bot_id/webhook', async (req, res) => {
		try {
			try { if (process.env['PG_URL']) await ensureSchema() } catch { }
			const expected = process.env['TG_SECRET_TOKEN']
			const header = String(req.header('X-Telegram-Bot-Api-Secret-Token') || '')
			if (expected && header !== expected) {
				log.warn({ have_header: !!header }, 'tg webhook: invalid secret token')
				return res.status(401).json({ error: 'invalid_secret' })
			}

			const bot_id = String(req.params['bot_id'])
			const update: any = req.body
			log.info({ bot_id, has_bus: !!bus, router_enabled: String(process.env['TG_ROUTER_ENABLED'] || '0') }, 'tg webhook: start')
			// Fast-path: handle /start <code> via legacy pairing even when TG_ROUTER_ENABLED=1
			try {
				const startText: string | undefined = update?.message?.text || update?.edited_message?.text || update?.callback_query?.data
				if (typeof startText === 'string' && startText.trim().startsWith('/start ')) {
					const payload = startText.trim().slice('/start '.length)
					if (!payload.startsWith('bind:')) {
						const code = payload
						const rec = await pairConfirm(code)
						const hubId = rec && rec.state === 'confirmed' ? (rec.hub_id || undefined) : undefined
						const chat_id = update?.message?.chat?.id || update?.edited_message?.chat?.id || update?.callback_query?.message?.chat?.id
						if (hubId && chat_id) {
							try { await tgLinkSet(hubId, String(chat_id), bot_id, String(chat_id)) } catch { }
							// Ensure alias binding exists for this chat
							try {
								const existing = await listBindings(Number(chat_id))
								let alias = 'hub'
								const names = new Set((existing || []).map(b => String(b.alias)))
								if (names.has(alias)) { let i = 2; while (names.has(`hub-${i}`)) i++; alias = `hub-${i}` }
								const makeDefault = (existing || []).length === 0
								await upsertBinding(Number(chat_id), hubId, alias, makeDefault)
								if (makeDefault) { try { await setSession(Number(chat_id), hubId, 'manual') } catch { } }
							} catch { }
							// Send quick ack to user
							try {
								if (bus) {
									const subject = `tg.output.${bot_id}.chat.${chat_id}`
									const out = { target: { bot_id, hub_id: hubId, chat_id: String(chat_id) }, messages: [{ type: 'text', text: 'Pair confirmed' }] }
									await bus.publishSubject(subject, out)
								} else {
									const token = process.env['TG_BOT_TOKEN'] || ''
									if (token) {
										const { TelegramSender } = await import('../telegram/sender.js')
										await new TelegramSender(token).send({ target: { bot_id, hub_id: hubId, chat_id: String(chat_id) }, messages: [{ type: 'text', text: 'Pair confirmed' }] } as any)
									}
								}
							} catch { }
						}
						return res.status(200).json({ ok: true, routed: false, diag: { step: 'fast_start', hub_id: hubId, chat_id: chat_id } })
					}
				}
			} catch { }
			// Optional: new multi-hub router (MVP) gate by env flag
			if ((process.env['TG_ROUTER_ENABLED'] || '0') === '1') {
				// Try router path; if it fails or does not route, fall back to classic publish
				try {
					await initTgRouting();
					log.info({ ok: true }, 'tg webhook: router init ok')
				} catch (e) {
					log.warn({ err: String(e) }, 'tg webhook: router init failed (will use classic path)')
				}
				try {
					const out = await onTelegramUpdate(bot_id, update)
					log.info({ status: out?.status, routed: out?.body?.routed }, 'tg webhook: router handled')
					if (out?.body?.routed === true) {
						return res.status(out.status).json(out.body)
					}
					log.info({ reason: 'not_routed' }, 'tg webhook: router fallback to classic path')
				} catch (e) {
					log.warn({ err: String(e) }, 'tg webhook: router handler failed (fallback to classic)')
				}
			}
			// minimal diagnostics for incoming webhook
			try {
				const updType = update?.message ? 'message' : (update?.edited_message ? 'edited_message' : (update?.callback_query ? 'callback_query' : 'unknown'))
				const chat_id = update?.message?.chat?.id || update?.edited_message?.chat?.id || update?.callback_query?.message?.chat?.id
				const user_id = update?.message?.from?.id || update?.edited_message?.from?.id || update?.callback_query?.from?.id
				const text = update?.message?.text || update?.edited_message?.text || update?.callback_query?.data
				log.info({ bot_id, updType, update_id: update?.update_id, chat_id: chat_id ? String(chat_id) : undefined, user_id: user_id ? String(user_id) : undefined, has_text: !!text }, 'tg webhook: received')
			} catch (e) {
				log.warn({ err: String(e) }, 'tg webhook: failed to log envelope')
			}
			const evt = toInputEvent(bot_id, update, null)
			tg_updates_total.inc({ type: evt.type })

			// idempotency
			const idemKey = `idem:tg:${bot_id}:${evt.update_id}`
			const cached = await idemGet(idemKey)
			if (cached) { log.info({ idemKey }, 'tg webhook: idem replay'); return res.status(cached.status).json(cached.body) }

			// media enrich
			const token = process.env['TG_BOT_TOKEN']
			if (token) {
				try {
					const maxMb = Number.parseInt(process.env['MAX_TG_FILE_MB'] || '20', 10)

					if (evt.type === 'audio' && evt.payload?.['file_id']) {
						const info = await getFilePath(token, evt.payload['file_id'])
						const fpath = typeof info === 'string' ? info : (info as any)?.file_path
						const fsz = (info as any)?.file_size as number | undefined

						if (fsz && fsz > maxMb * 1024 * 1024) {
							log.warn({ bot_id, update_id: evt.update_id, file_size: fsz }, 'audio too large, skip download')
						} else if (fpath) {
							const local = await downloadFile(token, fpath, bot_id)
							const wav = await convertOpusToWav16k(local)
								; (evt.payload as any).audio_path = wav || local
						}

					} else if (evt.type === 'photo' && evt.payload?.['file_id']) {
						const info = await getFilePath(token, evt.payload['file_id'])
						const fpath = typeof info === 'string' ? info : (info as any)?.file_path
						const fsz = (info as any)?.file_size as number | undefined

						if (fsz && fsz > maxMb * 1024 * 1024) {
							log.warn({ bot_id, update_id: evt.update_id, file_size: fsz }, 'photo too large, skip download')
						} else if (fpath) {
							const local = await downloadFile(token, fpath, bot_id)
								; (evt.payload as any).image_path = local
						}

					} else if (evt.type === 'document' && evt.payload?.['file_id']) {
						const info = await getFilePath(token, evt.payload['file_id'])
						const fpath = typeof info === 'string' ? info : (info as any)?.file_path
						const fsz = (info as any)?.file_size as number | undefined

						if (fsz && fsz > maxMb * 1024 * 1024) {
							log.warn({ bot_id, update_id: evt.update_id, file_size: fsz }, 'document too large, skip download')
						} else if (fpath) {
							const local = await downloadFile(token, fpath, bot_id)
								; (evt.payload as any).document_path = local
						}
					}
				} catch {
					/* non-fatal */
				}
			}

			// pairing via /start <code>
			if (evt.type === 'text') {
				const code = extractStartCode(evt.payload?.['text'])
				if (code) {
					let hubId: string | undefined

					// Local confirm: find locally issued pair and resolve hub_id
					const rec = await pairConfirm(code)
					if (rec && rec.state === 'confirmed') hubId = rec.hub_id || undefined
					else log.warn({ bot_id, update_id: evt.update_id }, 'local pairConfirm failed or missing hub_id')

					if (hubId) {
						try {
							const { bindingUpsert } = await import('../pairing/store.js')
							await bindingUpsert('telegram', evt.user_id, bot_id, hubId)
							// store simplified hub. chat link for outbound
							await tgLinkSet(hubId, String(evt.user_id), bot_id, String(evt.chat_id))
							log.info({ hub_id: hubId, bot_id, chat_id: String(evt.chat_id) }, 'telegram pairing linked')
							// Send welcome message right after successful pairing
							if (bus) {
								const subject = `tg.output.${bot_id}.chat.${evt.chat_id}`
								const welcome = process.env['TG_WELCOME_TEXT'] || 'Successfully paired. You can start messaging.'
								const out = {
									target: { bot_id, hub_id: hubId, chat_id: String(evt.chat_id) },
									messages: [{ type: 'text', text: welcome }],
								}
								try {
									await bus.publishSubject(subject, out)
									log.info({ subject, hub_id: hubId, chat_id: String(evt.chat_id) }, 'welcome sent via bus')
								} catch (e) {
									log.warn({ bot_id, update_id: evt.update_id, err: String(e) }, 'welcome publish via bus failed')
								}
								// Also send directly to Telegram to ensure user feedback
								if (!bus) {
									try {
										const token = process.env['TG_BOT_TOKEN'] || ''
										if (token) {
											const { TelegramSender } = await import('../telegram/sender.js')
											await new TelegramSender(token).send(out as any)
										}
									} catch { /* ignore */ }
								}
							}
						} catch (e) {
							log.warn({ bot_id, update_id: evt.update_id, err: String(e) }, 'bindingUpsert failed after confirm')
						}
					}
				}
			}

			// resolve hub
			const locale = (evt.payload as any)?.meta?.lang
			const hub = (await resolveHubId('telegram', evt.user_id, bot_id, locale)) || process.env['DEFAULT_HUB']
			log.info({ hub, user_id: evt.user_id, bot_id }, 'tg webhook: hub resolved')
			evt.hub_id = hub || null

			let status = 202
			let body: any = { ok: true, routed: false }
			if (hub && bus) {
				const envelope = {
					event_id: randomUUID().replace(/-/g, ''),
					kind: 'io.input',
					ts: new Date().toISOString(),
					dedup_key: `${bot_id}:${evt.update_id}`,
					payload: evt,
					meta: { bot_id, hub_id: hub, trace_id: randomUUID().replace(/-/g, ''), retries: 0 },
				}
				try {
					const subject = `tg.input.${hub}`
					log.info({ subject, event_id: envelope.event_id }, 'tg webhook: publishing to NATS')
					await bus.publish_input(hub, envelope)
					enqueue_total.inc({ hub })
					status = 200
					body = { ok: true, routed: true }
				} catch (e) {
					log.error({ bot_id, hub_id: hub, update_id: evt.update_id, err: String(e) }, 'publish_input failed')
					await bus.publish_dlq('input', { error: 'publish_failed', envelope })
				}
			} else if (hub) {
				// HTTP fallback to hub if bus is unavailable
				try {
					const base = process.env['HUB_BASE_URL'] || process.env['ADAOS_HUB_API_BASE'] // should include protocol and optional /api
					if (base) {
						const path = `/io/bus/tg.input.${hub}`
						const url = (new URL(path, base)).toString()
						const { request } = await import('undici')
						const token = process.env['ADAOS_TOKEN'] || ''
						const resp = await request(url, {
							method: 'POST',
							headers: { 'content-type': 'application/json', ...(token ? { 'X-AdaOS-Token': token } : {}) },
							body: JSON.stringify(evt),
						})
						if (resp.statusCode >= 200 && resp.statusCode < 300) {
							log.info({ url, hub, bot_id }, 'http fallback to hub: routed')
							status = 200
							body = { ok: true, routed: true }
						}
					}
				} catch (e) {
					log.error({ bot_id, hub_id: hub, update_id: evt.update_id, err: String(e) }, 'http fallback to hub failed')
				}
			}

			await idemPut(idemKey, { status, body }, 24 * 3600)
			return res.status(status).json(body)

		} catch (e) {
			if (bus) {
				try { await bus.publish_dlq('input', { error: String(e) }) } catch { }
			}
			return res.status(500).json({ ok: false })
		}
	})
}
