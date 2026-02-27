import type { Server as HttpServer } from 'node:http'
import { randomBytes } from 'node:crypto'
import net from 'node:net'
import pino from 'pino'
import { WebSocketServer } from 'ws'
import { verifyHubToken } from '../../db/tg.repo.js'
import { ws_nats_proxy_conn_close_total, ws_nats_proxy_conn_open_total, ws_nats_proxy_upstream_close_total } from '../telemetry.js'

// Keep logger lazy. This module is imported before `installRootLogCapture()` runs in `app.ts`,
// and pino's destination can bind to `fs.writeSync` early. Creating the logger lazily ensures
// dev log capture can still intercept ws-nats-proxy output via the fs hooks.
let _log: ReturnType<typeof pino> | null = null
function log() {
	if (_log) return _log
	_log = pino({ name: 'ws-nats-proxy' })
	return _log
}
const NATS_PING = Buffer.from('PING\r\n', 'utf8')
const NATS_PONG = Buffer.from('PONG\r\n', 'utf8')
const NATS_ERR = Buffer.from('-ERR', 'utf8')

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

function countMarkerWithTail(tail: Buffer, chunk: Buffer, marker: Buffer): { count: number; tail: Buffer } {
	// Count marker occurrences, including those split across chunk boundaries (using tail).
	// The tail is limited to (len-1) bytes, so it cannot contain a full marker and won't double-count.
	const combined = tail.length ? Buffer.concat([tail, chunk]) : chunk
	let count = 0
	let idx = 0
	while (true) {
		const at = combined.indexOf(marker, idx)
		if (at < 0) break
		count += 1
		idx = at + marker.length
	}
	const keep = Math.max(marker.length - 1, 0)
	const nextTail = keep > 0 ? combined.subarray(Math.max(combined.length - keep, 0)) : Buffer.alloc(0)
	return { count, tail: nextTail }
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

export function installWsNatsProxy(server: HttpServer) {
	const path = (process.env['WS_NATS_PATH'] || '/nats').trim() || '/nats'
	const upstream = parseNatsUrl(process.env['NATS_URL'] || 'nats://nats:4222')
	const verbose = (process.env['WS_NATS_PROXY_VERBOSE'] || '0') === '1'
	const pingTrace = (process.env['WS_NATS_PROXY_PING_TRACE'] || '0') === '1'
	const wsPingEnabled = (process.env['WS_NATS_PROXY_WS_PING'] || '0') === '1'
	log().info({ path, upstream: { host: upstream.host, port: upstream.port } }, 'install ws->nats proxy')

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
			log().warn({ err: String(e) }, 'ws-nats-proxy upgrade failed')
		}
	})

 wss.on('connection', (ws: any, req: any) => {
		const connId = randomBytes(4).toString('hex')
		const connTag = (() => {
			try {
				const v = req?.headers?.['x-adaos-nats-conn']
				if (typeof v === 'string' && v.trim()) return v.trim()
			} catch {}
			return null
		})()
		const rip = (req.headers['x-forwarded-for'] as string) || req.socket.remoteAddress || ''
		if (verbose) log().info({ conn: connId, from: rip, tag: connTag }, 'conn open')
		try {
			ws_nats_proxy_conn_open_total.inc()
		} catch {}

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
		let upstreamConnecting = false
		const upstreamPendingWrites: Buffer[] = []

		function closeBoth(code?: number, reason?: string) {
			try {
				ws.close(code || 1000, reason)
			} catch {}
			try {
				upstreamSock?.destroy()
			} catch {}
		}

		function writeUpstream(buf: Buffer, why: string) {
			if (!buf.length) return
			if (!upstreamSock || (upstreamSock as any).destroyed) {
				logSummary('upstream missing while writing', { why })
				closeBoth(1011, 'upstream_missing')
				return
			}
			// If the upstream connection is still in-flight, queue writes until `connect`.
			// Node will also buffer writes, but keeping an explicit queue avoids subtle races
			// with repeated `connectUpstream()` calls and makes diagnostics clearer.
			if (!connected) {
				upstreamPendingWrites.push(buf)
				return
			}
			try {
				const ok = upstreamSock.write(buf)
				if (ok === false) logSummary('upstream backpressure', { writableLength: upstreamSock.writableLength, why })
			} catch (e) {
				logSummary('upstream write failed', { err: String(e), why })
				closeBoth(1011, 'upstream_write_failed')
			}
		}

		function logSummary(event: string, extra?: Record<string, unknown>) {
			const base = {
				conn: connId,
				tag: connTag,
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
			}
			// Avoid log spam on expected closes; keep signal for abnormal drops.
			// - Clean shutdowns (1000/1001) are common on reload/navigation.
			// - 1006 indicates an abnormal close (no close frame) and is worth surfacing.
			const code = typeof (extra || {})?.['code'] === 'number' ? (extra as any).code : null
			const reason = typeof (extra || {})?.['reason'] === 'string' ? (extra as any).reason : null
			if (verbose) {
				log().info(base, event)
				return
			}
			// If we close the WS because the upstream NATS socket closed, we still want visibility even if
			// the WS close code is "clean". This is the dominant symptom for hub-side "UnexpectedEOF".
			if (event === 'conn close' && code === 1000 && reason && String(reason).includes('upstream_')) {
				log().warn(base, event)
				return
			}
			if (event === 'conn close' && (code === 1000 || code === 1001)) {
				log().debug(base, event)
				return
			}
			if (event === 'conn close' && code === 1006) {
				log().warn(base, event)
				return
			}
			log().info(base, event)
		}

		// Extra diagnostics: attach to the underlying TCP socket events to see whether the connection
		// is being cut at the transport layer (which shows up as WS close 1006 on the hub).
		try {
			const sock: any = (ws as any)?._socket
			if (sock && !sock.__adaos_ws_diag_attached) {
				sock.__adaos_ws_diag_attached = true
				const sockDiag = (extra?: Record<string, unknown>) => {
					try {
						return {
							socketDestroyed: Boolean(sock.destroyed),
							socketHadError: Boolean(sock.errored),
							socketBytesRead: Number(sock.bytesRead || 0),
							socketBytesWritten: Number(sock.bytesWritten || 0),
							remote: sock?.remoteAddress ? String(sock.remoteAddress) + ':' + String(sock.remotePort || '') : null,
							local: sock?.localAddress ? String(sock.localAddress) + ':' + String(sock.localPort || '') : null,
							...(extra || {}),
						}
					} catch {
						return extra || {}
					}
				}
				sock.on('end', () => logSummary('ws socket end', sockDiag()))
				sock.on('close', (hadErr: any) => logSummary('ws socket close', sockDiag({ hadError: Boolean(hadErr) })))
				sock.on('timeout', () => logSummary('ws socket timeout', sockDiag()))
				sock.on('error', (e: any) => logSummary('ws socket error', sockDiag({ err: String(e) })))
			}
		} catch {}

		function armWsPing() {
			if (!wsPingEnabled) return
			if (wsPingTimer) clearInterval(wsPingTimer)
			wsPingTimer = setInterval(() => {
				try {
					if (ws.readyState !== 1) return
					ws.ping()
					wsPingsSent += 1
					if (pingTrace) log().info({ conn: connId, hub_id: hubIdForLog }, 'ws ping (from proxy)')
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
					if (pingTrace) log().info({ conn: connId, hub_id: hubIdForLog }, 'nats ping (keepalive -> client)')
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
			// Never create more than one upstream socket per WS connection.
			// `connectUpstream()` is called on connect (to forward INFO) and again after auth (to ensure upstream exists).
			// Without this guard, a timing race can create two sockets and overwrite `upstreamSock`, leading to
			// protocol corruption and upstream-initiated closes that surface as "UnexpectedEOF" on the hub.
			if (upstreamSock && !(upstreamSock as any).destroyed) return
			if (upstreamConnecting) return
			upstreamConnecting = true
			const sock = net.createConnection({ host: upstream.host, port: upstream.port })
			upstreamSock = sock
			try {
				;(sock as any).setNoDelay?.(true)
			} catch {}
			try {
				sock.setKeepAlive(true, 20_000)
			} catch {}
			sock.on('connect', () => {
				connected = true
				upstreamConnecting = false
				try {
					// Flush any queued writes (CONNECT, subscriptions, etc).
					while (upstreamPendingWrites.length) {
						const chunk = upstreamPendingWrites.shift()!
						const ok = sock.write(chunk)
						if (ok === false) {
							logSummary('upstream backpressure on flush', { writableLength: sock.writableLength })
							break
						}
					}
				} catch (e) {
					logSummary('upstream flush failed', { err: String(e) })
					closeBoth(1011, 'upstream_flush_failed')
				}
			})
			sock.on('data', (chunk) => {
				bytesDown += chunk.length
				try {
					const at = chunk.indexOf(NATS_ERR)
					if (at >= 0) {
						// Keep a short excerpt for diagnostics (avoid dumping payloads).
						const raw = chunk.subarray(at, Math.min(at + 240, chunk.length)).toString('utf8')
						const line = raw.split('\r\n', 1)[0]
						logSummary('upstream -ERR', { line })
					}
				} catch {}
				const scanPing = countMarkerWithTail(upstreamTail, chunk, NATS_PING)
				upstreamTail = scanPing.tail
				if (scanPing.count > 0) {
					lastUpstreamPingAt = Date.now()
					// Be defensive: reply to upstream PINGs ourselves.
					// Some hubs/proxies may not reliably forward/respond to NATS PING/PONG, which can lead to
					// the server closing the TCP connection and the hub seeing "UnexpectedEOF".
					//
					// Extra PONGs are safe in the NATS protocol and help keep the upstream connection stable.
					try {
						for (let i = 0; i < scanPing.count; i += 1) {
							const ok = sock.write(NATS_PONG)
							proxySentPong += 1
							if (ok === false) {
								logSummary('upstream backpressure on PONG', { writableLength: sock.writableLength })
								break
							}
						}
					} catch (e) {
						logSummary('upstream PONG write failed', { err: String(e) })
						closeBoth(1011, 'upstream_pong_failed')
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
			sock.on('close', (hadError) => {
				upstreamConnecting = false
				connected = false
				logSummary('upstream close', { hadError: Boolean(hadError) })
				try {
					ws_nats_proxy_upstream_close_total.labels(hadError ? '1' : '0').inc()
				} catch {}
				closeBoth(1000, 'upstream_close')
			})
			sock.on('error', (err) => {
				upstreamConnecting = false
				connected = false
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
				log().warn({ from: rip, line: line.slice(0, 200) }, 'unexpected first line')
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
				log().warn({ from: rip, err: String(e) }, 'bad CONNECT json')
				closeBoth(1002, 'bad_connect_json')
				authInFlight = false
				return true
			}

			const userRaw = String(obj?.user || '')
			const passRaw = String(obj?.pass || '')
			if (!userRaw || !passRaw) {
				log().warn({ from: rip }, 'missing CONNECT credentials')
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
						log().warn({ from: rip, hub_id: hubId, user: userRaw, pass: mask(passRaw) }, 'auth failed')
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
							writeUpstream(rewritten, 'connect')
							// Send any bytes that arrived after CONNECT while auth was in flight.
							const queued = preHandshakeQueue.length ? Buffer.concat(preHandshakeQueue) : Buffer.alloc(0)
							preHandshakeQueue = []
							const tail = queued.length ? Buffer.concat([rest, queued]) : rest
							if (tail.length) {
								writeUpstream(tail, 'connect_tail')
							}
							clientBuf = Buffer.alloc(0)
							handshaked = true
							authInFlight = false
							if (verbose) log().info({ from: rip, hub_id: hubId }, 'auth ok')
						} catch (e) {
							log().warn({ err: String(e) }, 'write upstream failed')
							authInFlight = false
							closeBoth(1011, 'upstream_write_failed')
						}
					}, 0)
				})
				.catch((e) => {
					log().error({ from: rip, err: String(e) }, 'auth error')
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
						if (pingTrace) log().info({ conn: connId, hub_id: hubIdForLog }, 'nats pong (from client)')
					}
					// Track client PINGs too (nats-py sends these as keepalive).
					if (buf.includes(NATS_PING)) {
						lastClientPingAt = Date.now()
						clientSentPing += 1
						if (pingTrace) log().info({ conn: connId, hub_id: hubIdForLog }, 'nats ping (from client)')
					}
				} catch {}
				// The proxy itself responds to upstream `PING`s, so client `PONG`s are not required upstream.
				// Forward client PONGs to upstream (do NOT strip). If we strip and our upstream PING detection
				// ever misses (e.g. boundary split), NATS will close the connection leading to flaky UnexpectedEOFs.
				writeUpstream(buf, 'client_data')
				return
			}
			clientBuf = Buffer.concat([clientBuf, buf])
			tryProcessHandshake()
		})

		ws.on('ping', (data: any) => {
			if (!pingTrace) return
			try {
				const buf = toBuffer(data)
				log().info({ hub_id: hubIdForLog, len: buf.length }, 'ws ping (from client)')
			} catch {}
		})

		ws.on('pong', (data: any) => {
			wsPongsReceived += 1
			lastWsPongAt = Date.now()
			if (!pingTrace) return
			try {
				const buf = toBuffer(data)
				log().info({ conn: connId, hub_id: hubIdForLog, len: buf.length }, 'ws pong (to proxy ping)')
			} catch {}
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
			// Extra diagnostics for abnormal closes (1006 = no close frame).
			if (code === 1006) {
				try {
					const sock: any = (ws as any)?._socket
					logSummary('ws close 1006 diag', {
						socketDestroyed: sock ? Boolean(sock.destroyed) : null,
						socketHadError: sock ? Boolean(sock.errored) : null,
						socketBytesRead: sock ? Number(sock.bytesRead || 0) : null,
						socketBytesWritten: sock ? Number(sock.bytesWritten || 0) : null,
						remote: sock?.remoteAddress ? String(sock.remoteAddress) + ':' + String(sock.remotePort || '') : null,
						local: sock?.localAddress ? String(sock.localAddress) + ':' + String(sock.localPort || '') : null,
					})
				} catch {}
			}
			logSummary('conn close', { code, reason })
			try {
				ws_nats_proxy_conn_close_total.labels(String(code)).inc()
			} catch {}
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
