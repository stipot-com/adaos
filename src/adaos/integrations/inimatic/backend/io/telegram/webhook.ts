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
import { upsertBinding, listBindings, setSession, ensureHubToken, getByAlias, setDefault, getSession } from '../../db/tg.repo.js'
import { NatsBus } from '../bus/nats.js'
import { randomUUID } from 'crypto'
import { tg_updates_total, enqueue_total, dlq_total } from '../telemetry.js'
// Local pairing only: resolve hub_id by locally issued code (pairCreate/pairConfirm)

const log = pino({ name: 'tg-webhook' })

export function installTelegramWebhookRoutes(app: express.Express, bus: NatsBus | null) {
    // Internal file proxy: streams Telegram file by file_id (auth via X-AdaOS-Token)
    app.get('/internal/tg/file', async (req, res) => {
        try {
            const tokenHdr = String(req.header('X-AdaOS-Token') || '')
            const expect = process.env['ADAOS_TOKEN'] || ''
            if (!expect || tokenHdr !== expect) return res.status(401).json({ error: 'unauthorized' })
            const bot_id = String(req.query['bot_id'] || '')
            const file_id = String(req.query['file_id'] || '')
            const botToken = process.env['TG_BOT_TOKEN'] || ''
            if (!botToken || !file_id) return res.status(400).json({ error: 'bad_request' })
            const meta = await getFilePath(botToken, file_id)
            if (!meta || typeof meta === 'string') return res.status(404).json({ error: 'not_found' })
            const tmp = await downloadFile(botToken, meta.file_path, bot_id || 'default')
            const pathMod = await import('node:path')
            let mimeMod: any
            try { mimeMod = await (new Function('m', 'return import(m)'))('mime-types') } catch {}
            const name = pathMod.basename(String(meta.file_path || 'file'))
            const mime = (mimeMod && mimeMod.lookup ? mimeMod.lookup(name) : '') || 'application/octet-stream'
            res.setHeader('Content-Type', String(mime))
            res.setHeader('Content-Disposition', `attachment; filename="${name}"`)
            res.setHeader('X-File-Name', name)
            res.sendFile(tmp)
        } catch (e) {
            log.error({ err: String(e) }, 'tg file proxy failed')
            res.status(500).json({ error: 'internal' })
        }
    })
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
								try { await ensureHubToken(hubId) } catch {}
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
				// Validate NATS_URL early to avoid cryptic "Invalid URL" errors
				const natsUrl = String(process.env['NATS_URL'] || '')
				let natsUrlOk = true
				if (!natsUrl) {
					log.warn({ natsUrl }, 'tg webhook: router disabled (missing NATS_URL)')
					natsUrlOk = false
				} else {
					try { new URL(natsUrl) } catch (e) {
						log.warn({ natsUrl, err: String(e) }, 'tg webhook: NATS_URL invalid; router will be skipped')
						natsUrlOk = false
					}
				}
				try {
					if (natsUrlOk) {
						await initTgRouting();
						log.info({ ok: true }, 'tg webhook: router init ok')
					} else {
						throw new Error('router_init_skipped_invalid_nats_url')
					}
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
							try { await ensureHubToken(hubId) } catch {}
							log.info({ hub_id: hubId, bot_id, chat_id: String(evt.chat_id) }, 'telegram pairing linked')
							// Send welcome message right after successful pairing
                        if (bus) {
                            const subject = `tg.output.${bot_id}.chat.${evt.chat_id}`
                            const welcome = process.env['TG_WELCOME_TEXT'] || 'Successfully paired. You can start messaging.'
                            // Try to include alias for better UX
                            let alias: string | undefined
                            try {
                                const { listBindings } = await import('../../db/tg.repo.js')
                                const binds = await listBindings(Number(evt.chat_id))
                                alias = (binds || []).find(b => String(b.hub_id) === String(hubId))?.alias as any
                            } catch { }
                            const out = {
                                alias,
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

			// resolve hub (session/default)
			const locale = (evt.payload as any)?.meta?.lang
            const defaultHub = process.env['DEFAULT_HUB']
            let hubResolved = await resolveHubId('telegram', evt.user_id, bot_id, locale)
            let sessionHub: string | undefined
            try {
                const sess = await getSession(Number(evt.chat_id))
                if (sess?.current_hub_id) sessionHub = String(sess.current_hub_id)
            } catch {}
            // Prefer session hub over router fallback that equals DEFAULT_HUB
            let hub = hubResolved
            if (!hub || (defaultHub && hub === defaultHub)) {
                if (sessionHub) hub = sessionHub
            }
            if (!hub) hub = defaultHub
            // Optional address override: leading @<hub_id|alias> text -> route to that hub and strip the address token
            try {
                if (evt.type === 'text') {
                    const txt0: string = (((evt.payload as any)?.text ?? '') as string) + ''
                    const m = txt0.match(/^\s*@([A-Za-z0-9_\-]+)\s+(.*)$/)
                    if (m) {
                        const addr = m[1]
                        let routedHub = ''
                        if (addr.startsWith('sn_')) routedHub = addr
                        else {
                            try {
                                const bindings = await listBindings(Number(evt.chat_id))
                                const hit = (bindings || []).find(b => String(b.alias) === addr)
                                routedHub = hit ? String(hit.hub_id) : ''
                            } catch { routedHub = '' }
                        }
                        if (routedHub) {
                            const stripped: string = (m[2] ?? '') as string
                            ;(evt.payload as any).text = stripped
                            // Also adjust legacy mirror text via evt later
                            hub = routedHub
                            log.info({ hub, addr }, 'tg webhook: address override')
                            ;(evt as any).__addr_override = true
                        }
                    }
                }
            } catch {}
            log.info({ hub, user_id: evt.user_id, bot_id }, 'tg webhook: hub resolved')
            evt.hub_id = hub || null

            // Filter control commands handled by backend router; do not send to hub
                if (evt.type === 'text') {
                    const t = String((evt.payload as any)?.text || '').trim()
                    const lower = t.toLowerCase()
                    const isCtrl = lower === '/list' || lower === '/help' || lower.startsWith('/use ') || lower === '/current' || lower === '/default' || lower.startsWith('/alias') || lower === '/bind_here' || lower === '/unbind_here'
                    if (isCtrl) {
                    try {
                        if (lower.startsWith('/use ')) {
                            const alias = t.slice(5).trim()
                            if (alias) {
                                const rec = await getByAlias(Number(evt.chat_id), alias)
                                if (rec?.hub_id) {
                                    try { await setSession(Number(evt.chat_id), String(rec.hub_id), 'manual') } catch {}
                                    try { await setDefault(Number(evt.chat_id), alias) } catch {}
                                }
                            }
                        }
                    } catch {}
                    await idemPut(idemKey, { status: 200, body: { ok: true, routed: false, info: 'handled_by_router' } }, 24 * 3600)
                    return res.status(200).json({ ok: true, routed: false })
                } else if ((evt as any)?.payload && typeof (evt as any).payload === 'object') {
                    // Allow addressing in caption for media: reuse @addr parsing by mapping caption to text
                    const cap = String(((evt as any).payload as any)?.text || '').trim()
                    if (cap.startsWith('@')) {
                        const fakeTextEvt: any = { type: 'text', payload: { text: cap }, hub_id: hub, chat_id: evt.chat_id, user_id: evt.user_id, update_id: evt.update_id }
                        // Re-run minimal address override logic
                        const m = cap.match(/^\s*@([A-Za-z0-9_\-]+)\s+(.*)$/)
                        if (m) {
                            const addr = m[1]
                            let routedHub = ''
                            if (addr.startsWith('sn_')) routedHub = addr
                            else {
                                try {
                                    const bindings = await listBindings(Number(evt.chat_id))
                                    const hit = (bindings || []).find(b => String(b.alias) === addr)
                                    routedHub = hit ? String(hit.hub_id) : ''
                                } catch { routedHub = '' }
                            }
                            if (routedHub) {
                                (evt as any).payload.text = (m[2] ?? '') as string
                                hub = routedHub
                                ;(evt as any).__addr_override = true
                                log.info({ hub, addr }, 'tg webhook: address override (caption)')
                            }
                        }
                    }
                }
            }

            // Prevent sending unaddressed text to a hub not bound to this chat
            try {
                if (evt.type === 'text' && !(evt as any).__addr_override) {
                    const bindings = await listBindings(Number(evt.chat_id))
                    const allowed = (bindings || []).some(b => String(b.hub_id) === String(hub))
                    if (!allowed) {
                        log.info({ hub, chat_id: evt.chat_id }, 'tg webhook: drop unaddressed text to foreign hub')
                        await idemPut(idemKey, { status: 200, body: { ok: true, routed: false, info: 'foreign_hub_ignored' } }, 24 * 3600)
                        return res.status(200).json({ ok: true, routed: false })
                    }
                }
            } catch {}

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
					// Legacy mirror for hubs listening on io.tg.in.<hub>.text
					try {
						if (evt.type === 'text') {
							const legacy = {
								text: (evt.payload as any)?.text || '',
								chat_id: Number(evt.chat_id),
								tg_msg_id: Number((evt.payload as any)?.meta?.msg_id || 0),
								route: { via: 'session' },
								meta: { is_command: false },
							}
							const legacySubj = `io.tg.in.${hub}.text`
							await bus.publish_subject(legacySubj, legacy)
							log.info({ legacySubj, chat_id: legacy.chat_id }, 'tg webhook: legacy mirror published')
						}
					} catch { /* ignore legacy mirror errors */ }
					enqueue_total.inc({ hub })
					// Optional HTTP mirror to hub API even when NATS is available
					try {
						if ((process.env['HUB_HTTP_MIRROR'] || '0') === '1') {
							const base = process.env['HUB_BASE_URL'] || process.env['ADAOS_HUB_API_BASE']
							if (base) {
								const path = `/io/bus/tg.input.${hub}`
								const url = (new URL(path, base)).toString()
								const { request } = await import('undici')
								const token = process.env['ADAOS_TOKEN'] || ''
								await request(url, {
									method: 'POST',
									headers: { 'content-type': 'application/json', ...(token ? { 'X-AdaOS-Token': token } : {}) },
									body: JSON.stringify(envelope),
								})
								log.info({ url, hub }, 'http mirror to hub: posted')
							}
						}
					} catch { /* ignore mirror errors */ }
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
