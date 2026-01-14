import type { Server as HttpsServer } from 'https'
import net from 'node:net'
import pino from 'pino'
import { WebSocketServer } from 'ws'
import { verifyHubToken } from '../../db/tg.repo.js'

const log = pino({ name: 'ws-nats-proxy' })
const NATS_PING = Buffer.from('PING\r\n', 'utf8')
const NATS_PONG = Buffer.from('PONG\r\n', 'utf8')

type UpstreamOpts = {
	host: string
	port: number
	user: string
	pass: string
}

function parseNatsUrl(url: string): UpstreamOpts {
	try {
		const u = new URL(url)
		const host = u.hostname || 'nats'
		const port = u.port ? Number(u.port) : 4222
		const user = decodeURIComponent(u.username || process.env['NATS_USER'] || '')
		const pass = decodeURIComponent(u.password || process.env['NATS_PASS'] || '')
		return { host, port, user, pass }
	} catch {
		return {
			host: process.env['NATS_HOST'] || 'nats',
			port: Number(process.env['NATS_PORT'] || 4222),
			user: process.env['NATS_USER'] || '',
			pass: process.env['NATS_PASS'] || '',
		}
	}
}

function mask(tok: string): string {
	if (!tok) return tok
	if (tok.length <= 6) return '***'
	return tok.slice(0, 3) + '***' + tok.slice(-2)
}

function toBuffer(data: any): Buffer {
	if (Buffer.isBuffer(data)) return data
	if (typeof data === 'string') return Buffer.from(data, 'utf8')
	// ws can deliver ArrayBuffer/TypedArray/Buffer[] depending on environment and options
	try {
		if (data instanceof ArrayBuffer) return Buffer.from(new Uint8Array(data))
		// eslint-disable-next-line @typescript-eslint/no-unsafe-argument
		if (ArrayBuffer.isView(data)) return Buffer.from(data as Uint8Array)
	} catch {}
	try {
		if (Array.isArray(data)) {
			const parts = data.map((p: any) => toBuffer(p))
			return Buffer.concat(parts)
		}
	} catch {}
	try {
		return Buffer.from(String(data), 'utf8')
	} catch {
		return Buffer.alloc(0)
	}
}

function hasMarkerWithTail(tail: Buffer, chunk: Buffer, marker: Buffer): { hit: boolean; tail: Buffer } {
	// Search for marker possibly split across boundaries by keeping the last (len-1) bytes as tail.
	const combined = tail.length ? Buffer.concat([tail, chunk]) : chunk
	const hit = combined.indexOf(marker) >= 0
	const keep = Math.max(marker.length - 1, 0)
	const nextTail = keep > 0 ? combined.subarray(Math.max(combined.length - keep, 0)) : Buffer.alloc(0)
	return { hit, tail: nextTail }
}

function stripAll(buf: Buffer, marker: Buffer): { out: Buffer; count: number } {
	let count = 0
	let idx = 0
	const parts: Buffer[] = []
	while (true) {
		const at = buf.indexOf(marker, idx)
		if (at < 0) break
		if (at > idx) parts.push(buf.subarray(idx, at))
		idx = at + marker.length
		count += 1
	}
	if (idx === 0) return { out: buf, count: 0 }
	if (idx < buf.length) parts.push(buf.subarray(idx))
	return { out: Buffer.concat(parts), count }
}

export function installWsNatsProxy(server: HttpsServer) {
	const path = (process.env['WS_NATS_PATH'] || '/nats').trim() || '/nats'
	const upstream = parseNatsUrl(process.env['NATS_URL'] || 'nats://nats:4222')
	log.info({ path, upstream: { host: upstream.host, port: upstream.port } }, 'install ws->nats proxy')

	// IMPORTANT: keep this in `noServer` mode.
	// Attaching via `{ server }` registers a global `server.on('upgrade')` listener inside `ws`,
	// and a mis-match/race can cause `ws` to write `HTTP/1.1 400 Bad Request` on unrelated upgraded
	// sockets (e.g. /hubs/... proxied websockets), yielding "Invalid frame header" in browsers.
	const wss = new WebSocketServer({
		noServer: true,
		perMessageDeflate: false,
		handleProtocols: (protocols: Set<string>, _req: any) => {
			if (protocols.has('nats')) return 'nats'
			const first = protocols.values().next().value as string | undefined
			return first || false
		},
	})

	server.on('upgrade', (req: any, socket: any, head: any) => {
		try {
			const rawUrl = String(req?.url || '')
			const pathname = new URL(rawUrl, 'https://x').pathname
			const match = pathname === path || pathname.startsWith(`${path}/`)
			if (!match) return
			wss.handleUpgrade(req, socket, head, (ws: any) => wss.emit('connection', ws, req))
		} catch (e) {
			try {
				socket.destroy()
			} catch {}
			log.warn({ err: String(e) }, 'ws-nats-proxy upgrade failed')
		}
	})

	wss.on('connection', (ws: any, req: any) => {
		const rip = (req.headers['x-forwarded-for'] as string) || req.socket.remoteAddress || ''
		log.info({ from: rip }, 'conn open')

		let connected = false
		let handshaked = false
		let clientBuf = Buffer.alloc(0)
		let clientTail = Buffer.alloc(0)
		let upstreamCarry = Buffer.alloc(0) // keep last bytes to avoid splitting PING\r\n across chunks
		let bytesUp = 0
		let bytesDown = 0
		let lastUpstreamPingAt: number | null = null
		let lastClientPongAt: number | null = null
		let proxySentPong = 0
		const openedAt = Date.now()
		let upstreamSock: net.Socket | null = null

		function closeBoth(code?: number, reason?: string) {
			try {
				ws.close(code || 1000, reason)
			} catch {}
			try {
				upstreamSock?.destroy()
			} catch {}
		}

		function logSummary(event: string, extra?: Record<string, unknown>) {
			log.info(
				{
					from: rip,
					handshaked,
					connected,
					uptime_s: (Date.now() - openedAt) / 1000,
					bytesUp,
					bytesDown,
					lastUpstreamPingAgo_s: lastUpstreamPingAt ? (Date.now() - lastUpstreamPingAt) / 1000 : null,
					lastClientPongAgo_s: lastClientPongAt ? (Date.now() - lastClientPongAt) / 1000 : null,
					proxySentPong,
					...(extra || {}),
				},
				event,
			)
		}

		function connectUpstream() {
			if (connected) return
			upstreamSock = net.createConnection({ host: upstream.host, port: upstream.port })
			try {
				;(upstreamSock as any).setNoDelay?.(true)
			} catch {}
			upstreamSock.on('connect', () => {
				connected = true
			})
			upstreamSock.on('data', (chunk) => {
				bytesDown += chunk.length

				// Avoid splitting `PING\r\n` across chunks: keep the last 4 bytes aside.
				const combined = upstreamCarry.length ? Buffer.concat([upstreamCarry, chunk]) : chunk
				const keep = Math.min(4, combined.length)
				const sendable = combined.subarray(0, combined.length - keep)
				upstreamCarry = combined.subarray(combined.length - keep)

				// If upstream sends PINGs, respond immediately from the proxy (TCP-side client),
				// and strip `PING\r\n` from data forwarded to the WS client to prevent duplicate PONGs.
				const stripped = stripAll(sendable, NATS_PING)
				if (stripped.count > 0) {
					lastUpstreamPingAt = Date.now()
					try {
						// One PONG is enough even if we saw multiple PINGs in the same batch.
						upstreamSock?.write(NATS_PONG)
						proxySentPong += 1
					} catch {}
				}
				const payload = stripped.out
				try {
					// If WS backpressure builds up, upstream PONG can be lost and the client will disconnect.
					// Prefer failing fast with diagnostics rather than silently dropping frames.
					if (ws.readyState !== 1) {
						logSummary('ws not open while sending downstream', { wsReadyState: ws.readyState })
						closeBoth(1001, 'ws_not_open')
						return
					}
					if (payload.length === 0) return
					ws.send(payload, { binary: true }, (err: any) => {
						if (err) {
							logSummary('ws send downstream failed', { err: String(err) })
							closeBoth(1011, 'ws_send_failed')
						}
					})
				} catch (e) {
					logSummary('ws send downstream threw', { err: String(e) })
					closeBoth(1011, 'ws_send_throw')
				}
			})
			upstreamSock.on('close', (hadError) => {
				logSummary('upstream close', { hadError: Boolean(hadError) })
				closeBoth(1000, 'upstream_close')
			})
			upstreamSock.on('error', (err) => {
				logSummary('upstream error', { err: String(err) })
				closeBoth(1011, 'upstream_error')
			})
		}

		function tryProcessHandshake(): boolean {
			const raw = clientBuf.toString('utf8')
			const lineEnd = raw.indexOf('\r\n')
			if (lineEnd <= 0) return false

			const line = raw.slice(0, lineEnd)
			if (!line.startsWith('CONNECT ')) {
				log.warn({ from: rip, line: line.slice(0, 200) }, 'unexpected first line')
				closeBoth(1002, 'bad_client')
				return true
			}

			const rest = clientBuf.subarray(lineEnd + 2)
			let obj: any
			try {
				obj = JSON.parse(line.slice('CONNECT '.length))
			} catch (e) {
				log.warn({ from: rip, err: String(e) }, 'bad CONNECT json')
				closeBoth(1002, 'bad_connect_json')
				return true
			}

			const userRaw = String(obj?.user || '')
			const passRaw = String(obj?.pass || '')
			if (!userRaw || !passRaw) {
				log.warn({ from: rip }, 'missing CONNECT credentials')
				closeBoth(1008, 'missing_creds')
				return true
			}

			// user is expected to be: hub_<hub_id>  OR  hub-<hub_id>
			const hubId = userRaw.startsWith('hub_')
				? userRaw.slice(4)
				: userRaw.startsWith('hub-')
					? userRaw.slice(4)
					: userRaw

			verifyHubToken(hubId, passRaw)
				.then((ok) => {
					if (!ok) {
						log.warn({ from: rip, hub_id: hubId, user: userRaw, pass: mask(passRaw) }, 'auth failed')
						closeBoth(1008, 'auth_failed')
						return
					}

					const u: any = { ...obj }
					try {
						delete u.auth_token
					} catch {}
					try {
						delete u.jwt
					} catch {}
					try {
						delete u.nkey
					} catch {}
					try {
						delete u.sig
					} catch {}
					u.user = upstream.user
					u.pass = upstream.pass
					const rewritten = Buffer.from('CONNECT ' + JSON.stringify(u) + '\r\n', 'utf8')

					connectUpstream()
					setTimeout(() => {
						try {
							upstreamSock?.write(rewritten)
							if (rest.length) upstreamSock?.write(rest)
							clientBuf = Buffer.alloc(0)
							handshaked = true
							log.info({ from: rip, hub_id: hubId }, 'auth ok')
						} catch (e) {
							log.warn({ err: String(e) }, 'write upstream failed')
							closeBoth(1011, 'upstream_write_failed')
						}
					}, 0)
				})
				.catch((e) => {
					log.error({ from: rip, err: String(e) }, 'auth error')
					closeBoth(1011, 'auth_error')
				})

			return true
		}

		ws.on('message', (data: any) => {
			const buf = toBuffer(data)
			if (handshaked) {
				try {
					bytesUp += buf.length
					const scanPong = hasMarkerWithTail(clientTail, buf, NATS_PONG)
					clientTail = scanPong.tail
					if (scanPong.hit) {
						lastClientPongAt = Date.now()
					}
				} catch {}
				try {
					upstreamSock?.write(buf)
				} catch {}
				return
			}
			clientBuf = Buffer.concat([clientBuf, buf])
			tryProcessHandshake()
		})

		ws.on('error', (err: any) => {
			logSummary('ws error', { err: String(err) })
			closeBoth()
		})

		ws.on('close', (code: number, reasonBuf: any) => {
			const reason = (() => {
				try {
					return typeof reasonBuf === 'string'
						? reasonBuf
						: Buffer.isBuffer(reasonBuf)
							? (reasonBuf as Buffer).toString('utf8')
							: ''
				} catch {
					return ''
				}
			})()
			logSummary('conn close', { code, reason })
			try {
				upstreamSock?.destroy()
			} catch {}
		})

		connectUpstream()
	})
}
