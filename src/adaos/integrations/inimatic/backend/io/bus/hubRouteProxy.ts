import type { Server as HttpsServer } from 'https'
import type express from 'express'
import pino from 'pino'
import { randomUUID } from 'crypto'
import { NatsBus } from './nats.js'
import { verifyWebSessionJwt } from '../../sessionJwt.js'

type RedisLike = {
	get(key: string): Promise<string | null>
}

const log = pino({ name: 'hub-route-proxy' })

function maskToken(tok?: string | null): string | null {
	if (!tok) return null
	const s = String(tok)
	if (s.length <= 10) return '***'
	return `${s.slice(0, 5)}***${s.slice(-3)}`
}

const MAX_CHUNK_RAW = 300_000

function* chunkBuffer(buf: Buffer): Generator<Buffer> {
	for (let off = 0; off < buf.length; off += MAX_CHUNK_RAW) {
		yield buf.subarray(off, Math.min(buf.length, off + MAX_CHUNK_RAW))
	}
}

type ProxyOpts = {
	redis: RedisLike
	natsUrl: string
	sessionJwtSecret?: string
	allowCrossHubOwner?: boolean
}

function extractBearer(req: any): string | null {
	const h = String(req?.headers?.authorization || '')
	const m = h.match(/^Bearer\s+(.+)$/i)
	return m ? m[1].trim() : null
}

function extractToken(req: any): string | null {
	return (
		extractBearer(req) ||
		(String(req?.query?.token || '') || '').trim() ||
		(String(req?.query?.session_jwt || '') || '').trim() ||
		(String(req?.headers?.['x-session-jwt'] || '') || '').trim() ||
		null
	)
}

async function verifySessionJwt(
	redis: RedisLike,
	sessionJwt: string,
	sessionJwtSecret?: string
): Promise<{ owner_id?: string; browser_key_id?: string; sid?: string } | null> {
	if (!sessionJwt) return null
	if (sessionJwtSecret) {
		const jwt = await verifyWebSessionJwt({ secret: sessionJwtSecret, token: sessionJwt })
		if (jwt) return jwt
	}
	try {
		const raw = await redis.get(`session:jwt:${sessionJwt}`)
		if (!raw) return null
		const data = JSON.parse(raw)
		return data && typeof data === 'object' ? data : null
	} catch {
		return null
	}
}

function stripHubPrefix(pathname: string): string {
	// /hubs/:hubId/<rest> -> /<rest>
	return pathname.replace(/^\/hubs\/[^/]+/, '') || '/'
}

function normalizeHeaders(headers: any): Record<string, string> {
	const out: Record<string, string> = {}
	if (!headers || typeof headers !== 'object') return out
	for (const [k, v] of Object.entries(headers)) {
		const key = String(k).toLowerCase()
		if (
			key === 'host' ||
			key === 'connection' ||
			key === 'upgrade' ||
			key === 'sec-websocket-key' ||
			key === 'sec-websocket-version' ||
			key === 'sec-websocket-extensions'
		) {
			continue
		}
		if (typeof v === 'string') out[key] = v
		else if (Array.isArray(v)) out[key] = v.map(String).join(', ')
		else if (v != null) out[key] = String(v)
	}
	return out
}

async function natsRequest(
	bus: NatsBus,
	opts: {
		subjectToHub: string
		subjectToBrowser: string
		payload: any
		timeoutMs: number
	}
): Promise<any> {
	return new Promise((resolve, reject) => {
		const { subjectToHub, subjectToBrowser, payload, timeoutMs } = opts
		let done = false
		let sub: any = null
		const timer = setTimeout(() => {
			if (done) return
			done = true
			try {
				sub?.unsubscribe?.()
			} catch {}
			reject(new Error(`nats request timeout (waiting ${subjectToBrowser})`))
		}, timeoutMs)

		bus
			.subscribe(subjectToBrowser, async (_subject: string, data: Uint8Array) => {
				try {
					const txt = new TextDecoder().decode(data)
					const msg = JSON.parse(txt)

					// HTTP proxy expects only `http_resp`. If we get anything else on this subject,
					// ignore and keep waiting until timeout.
					if (msg?.t !== 'http_resp') {
						if ((process.env['ROUTE_PROXY_VERBOSE'] || '0') === '1') {
							log.warn(
								{ subject: subjectToBrowser, t: String(msg?.t || '') },
								'http proxy: ignoring unexpected reply'
							)
						}
						return
					}
					if (msg?.status == null) {
						if ((process.env['ROUTE_PROXY_VERBOSE'] || '0') === '1') {
							log.warn({ subject: subjectToBrowser }, 'http proxy: ignoring reply without status')
						}
						return
					}

					if (done) return
					done = true
					clearTimeout(timer)
					try {
						sub?.unsubscribe?.()
					} catch {}
					resolve(msg)
				} catch (e) {
					// Ignore invalid JSON frames on this subject and keep waiting.
					if ((process.env['ROUTE_PROXY_VERBOSE'] || '0') === '1') {
						log.warn({ subject: subjectToBrowser, err: String(e) }, 'http proxy: ignoring invalid reply')
					}
				}
			})
			.then((s) => {
				sub = s
				return bus.publish_subject(subjectToHub, payload)
			})
			.catch((e) => {
				if (done) return
				done = true
				clearTimeout(timer)
				try {
					sub?.unsubscribe?.()
				} catch {}
				reject(e)
			})
	})
}

export function installHubRouteProxy(
	app: express.Express,
	server: HttpsServer,
	opts: ProxyOpts
) {
	const allowCrossHubOwner =
		opts.allowCrossHubOwner ??
		(process.env['ALLOW_OWNER_HUB_ANY'] || '1') !== '0'
	const verbose = (process.env['ROUTE_PROXY_VERBOSE'] || '0') === '1'
	const bus = new NatsBus(opts.natsUrl)
	let busReady: Promise<void> | null = null

	function ensureBus(): Promise<void> {
		if (busReady) return busReady
		busReady = bus
			.connect()
			.then(() => {
				log.info({ nats: opts.natsUrl }, 'route bus connected')
			})
			.catch((e) => {
				busReady = null
				throw e
			})
		return busReady
	}

	// ---- HTTP proxy: /hubs/:hubId/api/... -> hub local http://127.0.0.1:8777/api/...
	app.all('/hubs/:hubId/api/*', async (req, res) => {
		try {
			const hubId = String(req.params.hubId || '').trim()
			if (!hubId) return res.status(400).json({ ok: false, error: 'hub_id_required' })

			const sessionJwt = extractToken(req)
			if (!sessionJwt) return res.status(401).json({ ok: false, error: 'unauthorized' })
			const session = await verifySessionJwt(
				opts.redis,
				sessionJwt,
				opts.sessionJwtSecret
			)
			if (!session) return res.status(401).json({ ok: false, error: 'unauthorized' })
			const ownerId = String(session.owner_id || '')
			if (!allowCrossHubOwner && ownerId && ownerId !== hubId) {
				if (verbose) log.warn({ hubId, ownerId }, 'http proxy: owner/hub mismatch; denying')
				return res.status(403).json({ ok: false, error: 'forbidden' })
			} else if (ownerId && ownerId !== hubId && verbose) {
				log.warn({ hubId, ownerId }, 'http proxy: owner/hub mismatch; allowing (ALLOW_OWNER_HUB_ANY)')
			}

			await ensureBus()

			const url = new URL(`https://x${req.originalUrl}`)
			const path = stripHubPrefix(url.pathname) // /api/...
			const key = `${hubId}--http--${randomUUID()}`
			const toHub = `route.to_hub.${key}`
			const toBrowser = `route.to_browser.${key}`
			if (verbose) {
				log.info(
					{
						hubId,
						key,
						method: String(req.method || 'GET').toUpperCase(),
						path,
						toHub,
						toBrowser,
					},
					'http proxy: send'
				)
			}

			// We only support JSON-ish bodies for MVP; if express.json parsed it, use it.
			let bodyB64: string | null = null
			if (req.method !== 'GET' && req.method !== 'HEAD') {
				try {
					const raw =
						typeof req.body === 'string'
							? req.body
							: Buffer.from(JSON.stringify(req.body ?? {}), 'utf8').toString('utf8')
					bodyB64 = Buffer.from(raw, 'utf8').toString('base64')
				} catch {
					bodyB64 = null
				}
			}

			const payload = {
				t: 'http',
				method: String(req.method || 'GET').toUpperCase(),
				path,
				search: url.search || '',
				headers: normalizeHeaders(req.headers),
				body_b64: bodyB64,
			}

			const reply = await natsRequest(bus, {
				subjectToHub: toHub,
				subjectToBrowser: toBrowser,
				payload,
				timeoutMs: 15000,
			})

			const status = Number(reply?.status || 502)
			const headers = reply?.headers && typeof reply.headers === 'object' ? reply.headers : {}
			const body = typeof reply?.body_b64 === 'string' ? reply.body_b64 : ''
			const isTrunc = reply?.truncated === true
			const errMsg = typeof reply?.err === 'string' ? reply.err : ''
			if (verbose) {
				log.info(
					{
						hubId,
						key,
						method: String(req.method || 'GET').toUpperCase(),
						path,
						status,
						err: errMsg ? errMsg.slice(0, 200) : '',
					},
					'http proxy: reply'
				)
			}
			if (errMsg && (process.env['ROUTE_PROXY_VERBOSE'] || '0') === '1') {
				log.warn(
					{
						hubId,
						method: String(req.method || 'GET').toUpperCase(),
						path,
						status,
						err: errMsg.slice(0, 500),
					},
					'http proxy: hub error'
				)
				try {
					res.setHeader('x-adaos-proxy-error', errMsg.slice(0, 200))
				} catch {}
			}

			for (const [k, v] of Object.entries(headers)) {
				const key = String(k).toLowerCase()
				if (key === 'transfer-encoding' || key === 'connection') continue
				try {
					res.setHeader(key, String(v))
				} catch {}
			}
			if (isTrunc) {
				try {
					res.setHeader('x-adaos-proxy-truncated', '1')
				} catch {}
			}
			const buf = body ? Buffer.from(body, 'base64') : Buffer.from('')
			return res.status(status).send(buf)
		} catch (e) {
			log.warn({ err: String(e), hubId: String(req?.params?.hubId || '') }, 'http proxy failed')
			return res.status(502).json({ ok: false, error: 'hub_unreachable' })
		}
	})

	// ---- WebSocket proxy: /hubs/:hubId/ws and /hubs/:hubId/yws/<room?>
	let WebSocketServerCtor: any
	let wss: any

	async function ensureWs(): Promise<void> {
		if (wss) return
		// eslint-disable-next-line no-new-func
		const mod: any = (new Function('m', 'return import(m)'))('ws')
		const m = await Promise.resolve(mod)
		WebSocketServerCtor = m.WebSocketServer || m.Server
		if (!WebSocketServerCtor) throw new Error('ws package missing WebSocketServer export')
		wss = new WebSocketServerCtor({ noServer: true, perMessageDeflate: false })

		wss.on('connection', async (ws: any, req: any, meta: any) => {
			const hubId = String(meta?.hubId || '')
			const dstPath = String(meta?.dstPath || '/ws')
			const sessionJwt = String(meta?.sessionJwt || '')
			const key = `${hubId}--${randomUUID().replace(/-/g, '')}`
			const toHub = `route.to_hub.${key}`
			const toBrowser = `route.to_browser.${key}`

			let sub: any = null
			const pendingChunks = new Map<
				string,
				{ kind: 'bin' | 'text'; total: number; parts: Array<Buffer | string> }
			>()
			try {
				if (verbose) {
					ws.on('error', (err: any) => {
						log.warn({ hubId, dstPath, key, err: String(err) }, 'ws client error')
					})
				}
				await ensureBus()
				sub = await bus.subscribe(toBrowser, async (_subject: string, data: Uint8Array) => {
					try {
						const txt = new TextDecoder().decode(data)
						const msg = JSON.parse(txt)
						if (msg?.t === 'chunk' && typeof msg.id === 'string') {
							const id = String(msg.id)
							const idx = Number(msg.idx || 0)
							const total = Number(msg.total || 0)
							const kind = msg.kind === 'text' ? 'text' : 'bin'
							if (!total || idx < 0 || idx >= total) return
							let entry = pendingChunks.get(id)
							if (!entry) {
								entry = { kind, total, parts: new Array(total) }
								pendingChunks.set(id, entry)
							}
							if (entry.total !== total || entry.kind !== kind) return
							if (kind === 'bin') {
								if (typeof msg.data_b64 !== 'string') return
								entry.parts[idx] = Buffer.from(msg.data_b64, 'base64')
							} else {
								if (typeof msg.data !== 'string') return
								entry.parts[idx] = msg.data
							}
							// Check completion
							for (let i = 0; i < entry.total; i++) {
								if (entry.parts[i] == null) return
							}
							pendingChunks.delete(id)
							if (kind === 'bin') {
								const bufs = entry.parts as Buffer[]
								ws.send(Buffer.concat(bufs), { binary: true })
							} else {
								const segs = entry.parts as string[]
								ws.send(segs.join(''))
							}
							return
						}
						if (msg?.t === 'frame') {
							if (msg.kind === 'bin' && typeof msg.data_b64 === 'string') {
								ws.send(Buffer.from(msg.data_b64, 'base64'), { binary: true })
							} else if (msg.kind === 'text' && typeof msg.data === 'string') {
								ws.send(msg.data)
							}
						} else if (msg?.t === 'close') {
							const errMsg = typeof msg?.err === 'string' ? String(msg.err) : ''
							if (verbose && errMsg) {
								log.warn({ hubId, dstPath, key, err: errMsg.slice(0, 500) }, 'ws upstream closed')
							}
							try {
								ws.close(errMsg ? 1011 : 1000, errMsg ? 'upstream_error' : 'upstream_close')
							} catch {}
						}
					} catch {
						// ignore
					}
				})

				// open
				try {
					if (verbose) {
						log.info(
							{
								hubId,
								dstPath,
								key,
								toHub,
								toBrowser,
								query: String(meta?.query || ''),
							},
							'ws tunnel: open'
						)
					}
					await bus.publish_subject(toHub, {
						t: 'open',
						proto: 'ws',
						path: dstPath,
						query: meta?.query || '',
						headers: {},
					})
				} catch (e) {
					if (verbose) log.warn({ hubId, dstPath, key, err: String(e) }, 'ws open publish failed')
					try {
						ws.close(1011, 'nats_open_failed')
					} catch {}
					return
				}

				ws.on('message', async (data: any, isBinary: boolean) => {
					try {
						if (isBinary) {
							const buf = Buffer.isBuffer(data) ? data : Buffer.from(data)
							if (buf.length > MAX_CHUNK_RAW) {
								const id = `c_${randomUUID().replace(/-/g, '')}`
								const chunks = Array.from(chunkBuffer(buf))
								for (let i = 0; i < chunks.length; i++) {
									await bus.publish_subject(toHub, {
										t: 'chunk',
										id,
										kind: 'bin',
										idx: i,
										total: chunks.length,
										data_b64: chunks[i].toString('base64'),
									})
								}
							} else {
								await bus.publish_subject(toHub, {
									t: 'frame',
									kind: 'bin',
									data_b64: buf.toString('base64'),
								})
							}
						} else {
							const text = typeof data === 'string' ? data : Buffer.from(data).toString('utf8')
							if (text.length > MAX_CHUNK_RAW) {
								const id = `c_${randomUUID().replace(/-/g, '')}`
								const parts: string[] = []
								for (let off = 0; off < text.length; off += MAX_CHUNK_RAW) {
									parts.push(text.slice(off, off + MAX_CHUNK_RAW))
								}
								for (let i = 0; i < parts.length; i++) {
									await bus.publish_subject(toHub, {
										t: 'chunk',
										id,
										kind: 'text',
										idx: i,
										total: parts.length,
										data: parts[i],
									})
								}
							} else {
								await bus.publish_subject(toHub, {
									t: 'frame',
									kind: 'text',
									data: text,
								})
							}
						}
					} catch {}
				})

				ws.on('close', async (code: number, reason: Buffer) => {
					if (verbose) {
						let r: string | null = null
						try {
							r = reason ? reason.toString('utf8') : ''
						} catch {
							r = null
						}
						log.info({ hubId, dstPath, key, code, reason: r }, 'ws client close')
					}
					try {
						pendingChunks.clear()
						sub?.unsubscribe?.()
					} catch {}
					try {
						await bus.publish_subject(toHub, { t: 'close' })
					} catch {}
				})
			} catch (e) {
				try {
					sub?.unsubscribe?.()
				} catch {}
				try {
					ws.close(1011, 'proxy_error')
				} catch {}
				log.warn({ err: String(e), hubId, dstPath, key }, 'ws proxy setup failed')
			}
		})
	}

	// Ensure we get first shot at matching /hubs/:hubId/(ws|yws) before other upgrade handlers (e.g. socket.io).
	server.prependListener('upgrade', async (req: any, socket: any, head: any) => {
		try {
			const u = new URL(req.url, 'https://x')
			const m = u.pathname.match(/^\/hubs\/([^/]+)\/(ws|yws)(?:\/(.*))?$/)
			if (!m) return

			const hubId = decodeURIComponent(m[1] || '')
			const kind = m[2]
			const room = m[3] ? `/${m[3]}` : ''

			const sessionJwt = extractToken({
				headers: req.headers,
				query: Object.fromEntries(u.searchParams.entries()),
			})
			if (!sessionJwt) {
				if (verbose) log.warn({ path: u.pathname }, 'ws upgrade: missing token')
				try {
					socket.write(
						'HTTP/1.1 401 Unauthorized\r\n' +
							'Connection: close\r\n' +
							'Content-Type: text/plain\r\n' +
							'\r\n' +
							'missing token'
					)
				} catch {}
				socket.destroy()
				return
			}
			const session = await verifySessionJwt(
				opts.redis,
				sessionJwt,
				opts.sessionJwtSecret
			)
			if (!session) {
				if (verbose) {
					let rawLen: number | null = null
					try {
						const raw = await opts.redis.get(`session:jwt:${sessionJwt}`)
						rawLen = raw ? raw.length : 0
					} catch {
						rawLen = null
					}
					log.warn(
						{
							hubId,
							kind,
							path: u.pathname,
							token: maskToken(sessionJwt),
							tokenKind: sessionJwt.includes('.') ? 'jwt' : 'opaque',
							hasSecret: Boolean(opts.sessionJwtSecret),
							redisLen: rawLen,
						},
						'ws upgrade: invalid session'
					)
				}
				try {
					socket.write(
						'HTTP/1.1 401 Unauthorized\r\n' +
							'Connection: close\r\n' +
							'Content-Type: text/plain\r\n' +
							'\r\n' +
							'invalid session'
					)
				} catch {}
				socket.destroy()
				return
			}
			const ownerId = String(session.owner_id || '')
			if (!allowCrossHubOwner && ownerId && ownerId !== hubId) {
				if (verbose) log.warn({ hubId, ownerId, kind }, 'ws upgrade: owner/hub mismatch; denying')
				socket.destroy()
				return
			} else if (ownerId && ownerId !== hubId && verbose) {
				log.warn({ hubId, ownerId, kind }, 'ws upgrade: owner/hub mismatch; allowing (ALLOW_OWNER_HUB_ANY)')
			}

			await ensureWs()

			const dstPath = kind === 'ws' ? '/ws' : `/yws${room}`
			const query = u.search || ''
			if (verbose) log.info({ hubId, kind, dstPath }, 'ws upgrade: accepted')
			wss.handleUpgrade(req, socket, head, (ws: any) => {
				wss.emit('connection', ws, req, { hubId, dstPath, sessionJwt, query })
			})
		} catch {
			try {
				socket.destroy()
			} catch {}
		}
	})
}
