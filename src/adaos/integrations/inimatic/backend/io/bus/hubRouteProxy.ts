import type { Server as HttpsServer } from 'https'
import type express from 'express'
import pino from 'pino'
import { randomUUID } from 'crypto'
import { NatsBus } from './nats.js'

type RedisLike = {
	get(key: string): Promise<string | null>
}

const log = pino({ name: 'hub-route-proxy' })

type ProxyOpts = {
	redis: RedisLike
	natsUrl: string
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
	sessionJwt: string
): Promise<{ owner_id?: string; browser_key_id?: string; sid?: string } | null> {
	if (!sessionJwt) return null
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
			reject(new Error('nats request timeout'))
		}, timeoutMs)

		bus
			.subscribe(subjectToBrowser, async (_subject: string, data: Uint8Array) => {
				if (done) return
				done = true
				clearTimeout(timer)
				try {
					sub?.unsubscribe?.()
				} catch {}
				try {
					const txt = new TextDecoder().decode(data)
					resolve(JSON.parse(txt))
				} catch (e) {
					reject(e)
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
		opts.allowCrossHubOwner ?? (process.env['ALLOW_OWNER_HUB_ANY'] || '0') === '1'
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
			const session = await verifySessionJwt(opts.redis, sessionJwt)
			if (!session) return res.status(401).json({ ok: false, error: 'unauthorized' })
			const ownerId = String(session.owner_id || '')
			if (!allowCrossHubOwner && ownerId && ownerId !== hubId) {
				return res.status(403).json({ ok: false, error: 'forbidden' })
			}

			await ensureBus()

			const url = new URL(`https://x${req.originalUrl}`)
			const path = stripHubPrefix(url.pathname) // /api/...
			const key = `${hubId}--http--${randomUUID()}`
			const toHub = `route.to_hub.${key}`
			const toBrowser = `route.to_browser.${key}`

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
			log.warn({ err: String(e) }, 'http proxy failed')
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
			try {
				await ensureBus()
				sub = await bus.subscribe(toBrowser, async (_subject: string, data: Uint8Array) => {
					try {
						const txt = new TextDecoder().decode(data)
						const msg = JSON.parse(txt)
						if (msg?.t === 'frame') {
							if (msg.kind === 'bin' && typeof msg.data_b64 === 'string') {
								ws.send(Buffer.from(msg.data_b64, 'base64'), { binary: true })
							} else if (msg.kind === 'text' && typeof msg.data === 'string') {
								ws.send(msg.data)
							}
						} else if (msg?.t === 'close') {
							try {
								ws.close(1000, 'upstream_close')
							} catch {}
						}
					} catch {
						// ignore
					}
				})

				// open
				await bus.publish_subject(toHub, {
					t: 'open',
					proto: 'ws',
					path: dstPath,
					query: meta?.query || '',
					headers: {},
				})

				ws.on('message', async (data: any, isBinary: boolean) => {
					try {
						if (isBinary) {
							const buf = Buffer.isBuffer(data) ? data : Buffer.from(data)
							await bus.publish_subject(toHub, {
								t: 'frame',
								kind: 'bin',
								data_b64: buf.toString('base64'),
							})
						} else {
							const text = typeof data === 'string' ? data : Buffer.from(data).toString('utf8')
							await bus.publish_subject(toHub, {
								t: 'frame',
								kind: 'text',
								data: text,
							})
						}
					} catch {}
				})

				ws.on('close', async () => {
					try {
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
				log.warn({ err: String(e), hubId, dstPath }, 'ws proxy setup failed')
			}
		})
	}

	server.on('upgrade', async (req: any, socket: any, head: any) => {
		try {
			const u = new URL(req.url, 'https://x')
			const m = u.pathname.match(/^\/hubs\/([^/]+)\/(ws|yws)(?:\/(.*))?$/)
			if (!m) return

			const hubId = decodeURIComponent(m[1] || '')
			const kind = m[2]
			const room = m[3] ? `/${m[3]}` : ''

			const sessionJwt = extractToken({ headers: req.headers, query: Object.fromEntries(u.searchParams.entries()) })
			if (!sessionJwt) {
				socket.destroy()
				return
			}
			const session = await verifySessionJwt(opts.redis, sessionJwt)
			if (!session) {
				socket.destroy()
				return
			}
			const ownerId = String(session.owner_id || '')
			if (!allowCrossHubOwner && ownerId && ownerId !== hubId) {
				socket.destroy()
				return
			}

			await ensureWs()

			const dstPath = kind === 'ws' ? '/ws' : `/yws${room}`
			const query = u.search || ''
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
