import express from 'express'
import pino from 'pino'
import { toInputEvent } from './normalize.js'
import { getFilePath, downloadFile, convertOpusToWav16k } from './media.js'
import { resolveHubId } from '../router/resolve.js'
import { idemGet, idemPut } from '../idem/kv.js'
import { extractStartCode } from './pairing.js'
import { pairConfirm } from '../pairing/store.js'
import { NatsBus } from '../bus/nats.js'
import { randomUUID } from 'crypto'
import { tg_updates_total, enqueue_total, dlq_total } from '../telemetry.js'
import { loadRootSettings, makeRootDispatcher } from '../root/settings.js'

const log = pino({ name: 'tg-webhook' })

export function installTelegramWebhookRoutes(app: express.Express, bus: NatsBus | null) {
	app.post('/io/tg/:bot_id/webhook', async (req, res) => {
		try {
			const expected = process.env['TG_SECRET_TOKEN']
			const header = String(req.header('X-Telegram-Bot-Api-Secret-Token') || '')
			if (expected && header !== expected) {
				return res.status(401).json({ error: 'invalid_secret' })
			}

			const bot_id = String(req.params['bot_id'])
			const update = req.body
			const evt = toInputEvent(bot_id, update, null)
			tg_updates_total.inc({ type: evt.type })

			// idempotency
			const idemKey = `idem:tg:${bot_id}:${evt.update_id}`
			const cached = await idemGet(idemKey)
			if (cached) return res.status(cached.status).json(cached.body)

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

					// Prefer external RootSettings endpoint if configured
					const rs = loadRootSettings()
					if (rs) {
						try {
							const url = new URL(rs.tgPairingPath, rs.baseUrl).toString()
							const dispatcher = makeRootDispatcher(rs)
							const { request } = await import('undici')
							const resp = await request(url, {
								method: 'POST',
								headers: { 'content-type': 'application/json' },
								body: JSON.stringify({ code, user_id: evt.user_id, bot_id }),
								dispatcher,
							})
							if (resp.statusCode >= 200 && resp.statusCode < 300) {
								const data: any = await resp.body.json()
								hubId = data?.hub_id || undefined
							} else {
								log.warn({ bot_id, update_id: evt.update_id, status: resp.statusCode }, 'remote pairConfirm failed')
							}
						} catch (e) {
							log.error({ bot_id, update_id: evt.update_id, err: String(e) }, 'remote pairConfirm error')
						}
					} else {
						const rec = await pairConfirm(code)
						if (rec && rec.state === 'confirmed') hubId = rec.hub_id || undefined
					}

					if (hubId) {
						try {
							const { bindingUpsert } = await import('../pairing/store.js')
							await bindingUpsert('telegram', evt.user_id, bot_id, hubId)
							// Send welcome message right after successful pairing
							if (bus) {
								const subject = `tg.output.${bot_id}.chat.${evt.chat_id}`
								const welcome = process.env['TG_WELCOME_TEXT'] || '✅ Successfully paired. You can start messaging.'
								const out = {
									target: { bot_id, hub_id: hubId, chat_id: String(evt.chat_id) },
									messages: [{ type: 'text', text: welcome }],
								}
								try {
									await bus.publishSubject(subject, out)
								} catch (e) {
									log.warn({ bot_id, update_id: evt.update_id, err: String(e) }, 'welcome publish via bus failed')
								}
								// Also send directly to Telegram to ensure user feedback
								try {
									const token = process.env['TG_BOT_TOKEN'] || ''
									if (token) {
										const { TelegramSender } = await import('../telegram/sender.js')
										await new TelegramSender(token).send(out as any)
									}
								} catch { /* ignore */ }
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
					await bus.publish_input(hub, envelope)
					enqueue_total.inc({ hub })
					status = 200
					body = { ok: true, routed: true }
				} catch (e) {
					log.error({ bot_id, hub_id: hub, update_id: evt.update_id, err: String(e) }, 'publish_input failed')
					await bus.publish_dlq('input', { error: 'publish_failed', envelope })
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
