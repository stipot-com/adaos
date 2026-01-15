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
		let hubIdForLog: string | null = null
		let authInFlight = false
		let clientBuf = Buffer.alloc(0)
		let preHandshakeQueue: Buffer[] = []
		let clientTail = Buffer.alloc(0)
		let upstreamTail = Buffer.alloc(0)
		let bytesUp = 0
		let bytesDown = 0
		let lastUpstreamPingAt: number | null = null
		let lastClientPongAt: number | null = null
		let lastClientPingAt: number | null = null
		let lastUpstreamPongAt: number | null = null
		let proxySentPong = 0
		let clientSentPong = 0
		let clientSentPing = 0
		let upstreamSentPong = 0
		const openedAt = Date.now()
		let upstreamSock: net.Socket | null = null
		let wsPingTimer: NodeJS.Timeout | null = null
		let natsKeepaliveTimer: NodeJS.Timeout | null = null
		let wsPingsSent = 0
		let wsPongsReceived = 0
		let lastWsPongAt: number | null = null
		let natsKeepalivesSent = 0

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
					hub_id: hubIdForLog,
					handshaked,
					connected,
					uptime_s: (Date.now() - openedAt) / 1000,
					bytesUp,
					bytesDown,
					lastUpstreamPingAgo_s: lastUpstreamPingAt ? (Date.now() - lastUpstreamPingAt) / 1000 : null,
					lastClientPongAgo_s: lastClientPongAt ? (Date.now() - lastClientPongAt) / 1000 : null,
					lastClientPingAgo_s: lastClientPingAt ? (Date.now() - lastClientPingAt) / 1000 : null,
					lastUpstreamPongAgo_s: lastUpstreamPongAt ? (Date.now() - lastUpstreamPongAt) / 1000 : null,
					proxySentPong,
					clientSentPong,
					clientSentPing,
					upstreamSentPong,
					wsPingsSent,
					wsPongsReceived,
					lastWsPongAgo_s: lastWsPongAt ? (Date.now() - lastWsPongAt) / 1000 : null,
					natsKeepalivesSent,
					...(extra || {}),
				},
				event,
			)
		}

		function armWsPing() {
			if (wsPingTimer) clearInterval(wsPingTimer)
			wsPingTimer = setInterval(() => {
				try {
					if (ws.readyState !== 1) return
					ws.ping()
					wsPingsSent += 1
				} catch {}
			}, 25_000)
		}

		function armNatsKeepalive() {
			if (natsKeepaliveTimer) clearInterval(natsKeepaliveTimer)
			// Many NATs/firewalls time out idle outbound mappings. WS control frames may be ignored by
			// intermediaries, so we send a tiny NATS protocol keepalive as *data* to the client.
			// The hub's nats client will respond with `PONG`, which creates outbound traffic hub->root.
			natsKeepaliveTimer = setInterval(() => {
				try {
					if (!handshaked) return
					if (ws.readyState !== 1) return
					ws.send(NATS_PING, { binary: true })
					natsKeepalivesSent += 1
				} catch {}
			}, 20_000)
		}

		function disarmNatsKeepalive() {
			try {
				if (natsKeepaliveTimer) clearInterval(natsKeepaliveTimer)
			} catch {}
			natsKeepaliveTimer = null
		}

		function disarmWsPing() {
			try {
				if (wsPingTimer) clearInterval(wsPingTimer)
			} catch {}
			wsPingTimer = null
		}

		function connectUpstream() {
			if (connected) return
			upstreamSock = net.createConnection({ host: upstream.host, port: upstream.port })
			try {
				;(upstreamSock as any).setNoDelay?.(true)
			} catch {}
			try {
				upstreamSock.setKeepAlive(true, 20_000)
			} catch {}
			upstreamSock.on('connect', () => {
				connected = true
			})
			upstreamSock.on('data', (chunk) => {
				bytesDown += chunk.length
				const scan = hasMarkerWithTail(upstreamTail, chunk, NATS_PING)
				upstreamTail = scan.tail
				if (scan.hit) {
					lastUpstreamPingAt = Date.now()
					try {
						const ok = upstreamSock?.write(NATS_PONG)
						if (ok === false) {
							logSummary('upstream backpressure on PONG', { writableLength: upstreamSock?.writableLength })
						}
						proxySentPong += 1
					} catch (e) {
						logSummary('upstream write PONG failed', { err: String(e) })
						closeBoth(1011, 'upstream_write_failed')
						return
					}
				}
				try {
					if (chunk.includes(NATS_PONG)) {
						lastUpstreamPongAt = Date.now()
						upstreamSentPong += 1
					}
				} catch {}
				try {
					// If WS backpressure builds up, upstream PONG can be lost and the client will disconnect.
					// Prefer failing fast with diagnostics rather than silently dropping frames.
					if (ws.readyState !== 1) {
						logSummary('ws not open while sending downstream', { wsReadyState: ws.readyState })
						closeBoth(1001, 'ws_not_open')
						return
					}
					ws.send(chunk, { binary: true }, (err: any) => {
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
			if (handshaked || authInFlight) return false
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
			clientBuf = Buffer.alloc(0)
			authInFlight = true
			let obj: any
			try {
				obj = JSON.parse(line.slice('CONNECT '.length))
			} catch (e) {
				log.warn({ from: rip, err: String(e) }, 'bad CONNECT json')
				closeBoth(1002, 'bad_connect_json')
				authInFlight = false
				return true
			}

			const userRaw = String(obj?.user || '')
			const passRaw = String(obj?.pass || '')
			if (!userRaw || !passRaw) {
				log.warn({ from: rip }, 'missing CONNECT credentials')
				closeBoth(1008, 'missing_creds')
				authInFlight = false
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
						authInFlight = false
						return
					}

					hubIdForLog = hubId

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
							const ok1 = upstreamSock?.write(rewritten)
							if (ok1 === false) logSummary('upstream backpressure on CONNECT', { writableLength: upstreamSock?.writableLength })
							// Send any bytes that arrived after CONNECT while auth was in flight.
							const queued = preHandshakeQueue.length ? Buffer.concat(preHandshakeQueue) : Buffer.alloc(0)
							preHandshakeQueue = []
							const tail = queued.length ? Buffer.concat([rest, queued]) : rest
							if (tail.length) {
								const ok2 = upstreamSock?.write(tail)
								if (ok2 === false) logSummary('upstream backpressure on CONNECT tail', { writableLength: upstreamSock?.writableLength })
							}
							clientBuf = Buffer.alloc(0)
							handshaked = true
							authInFlight = false
							log.info({ from: rip, hub_id: hubId }, 'auth ok')
						} catch (e) {
							log.warn({ err: String(e) }, 'write upstream failed')
							authInFlight = false
							closeBoth(1011, 'upstream_write_failed')
						}
					}, 0)
				})
				.catch((e) => {
					log.error({ from: rip, err: String(e) }, 'auth error')
					authInFlight = false
					closeBoth(1011, 'auth_error')
				})

			return true
		}

		ws.on('message', (data: any) => {
			let buf = toBuffer(data)
			if (authInFlight && !handshaked) {
				// Prevent double CONNECT parsing when the client keeps sending data while auth is in-flight.
				preHandshakeQueue.push(buf)
				return
			}
			if (handshaked) {
				try {
					bytesUp += buf.length
					const scanPong = hasMarkerWithTail(clientTail, buf, NATS_PONG)
					clientTail = scanPong.tail
					if (scanPong.hit) {
						lastClientPongAt = Date.now()
						clientSentPong += 1
					}
					// Track client PINGs too (nats-py sends these as keepalive).
					if (buf.includes(NATS_PING)) {
						lastClientPingAt = Date.now()
						clientSentPing += 1
					}
				} catch {}
				// The proxy itself responds to upstream `PING`s, so client `PONG`s are not required upstream.
				// Stripping them avoids sending unsolicited PONGs to upstream and keeps outbound traffic intact.
				try {
					const stripped = stripAll(buf, NATS_PONG)
					if (stripped.count > 0) buf = stripped.out
				} catch {}
				try {
					if (!upstreamSock || (upstreamSock as any).destroyed) {
						logSummary('upstream missing while writing', {})
						closeBoth(1011, 'upstream_missing')
						return
					}
					if (buf.length === 0) return
					const ok = upstreamSock.write(buf)
					if (ok === false) {
						logSummary('upstream backpressure', { writableLength: upstreamSock.writableLength })
					}
				} catch (e) {
					logSummary('upstream write failed', { err: String(e) })
					closeBoth(1011, 'upstream_write_failed')
				}
				return
			}
			clientBuf = Buffer.concat([clientBuf, buf])
			tryProcessHandshake()
		})

		ws.on('pong', () => {
			wsPongsReceived += 1
			lastWsPongAt = Date.now()
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
			disarmWsPing()
			disarmNatsKeepalive()
			try {
				upstreamSock?.destroy()
			} catch {}
		})

		// Keep the WS tunnel alive even if the NATS protocol is temporarily idle, otherwise
		// intermediaries may cut the connection (common 60–120s idle timeouts).
		armWsPing()
		armNatsKeepalive()
		connectUpstream()
	})
}
