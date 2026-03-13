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
const AUTH_VERIFY_PENDING_MS = Math.max(
	100,
	Number.parseInt(process.env['WS_NATS_PROXY_AUTH_VERIFY_PENDING_MS'] || '1000', 10) || 1000
)
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

function extractNatsCmdSubject(head: string, cmd: 'MSG' | 'PUB' | 'SUB'): string | null {
	try {
		const needle = `${cmd} `
		let idx = head.indexOf(needle)
		while (idx >= 0) {
			// Avoid false positives inside payloads: require start-of-buffer or start-of-line.
			if (idx === 0 || head[idx - 1] === '\n') break
			idx = head.indexOf(needle, idx + needle.length)
		}
		if (idx < 0) return null
		const lineEnd = head.indexOf('\n', idx)
		const line = (lineEnd >= 0 ? head.slice(idx, lineEnd) : head.slice(idx, idx + 240)).trim()
		const parts = line.split(/\s+/g)
		if (parts.length < 2) return null
		return String(parts[1] || '')
	} catch {
		return null
	}
}

function extractNatsCmdSubjects(head: string, cmd: 'MSG' | 'PUB' | 'SUB', maxSubjects = 8): string[] {
	try {
		const out: string[] = []
		const seen = new Set<string>()
		const re = new RegExp(`(?:^|\\n)${cmd} ([^\\s\\r\\n]+)`, 'g')
		for (;;) {
			const match = re.exec(head)
			if (!match) break
			const subj = String(match[1] || '')
			if (!subj || seen.has(subj)) continue
			seen.add(subj)
			out.push(subj)
			if (out.length >= maxSubjects) break
		}
		return out
	} catch {
		return []
	}
}

function extractRouteMsgSubjects(head: string, prefix: string, maxSubjects = 8): string[] {
	try {
		return extractNatsCmdSubjects(head, 'MSG', maxSubjects).filter((subj) => subj.startsWith(prefix))
	} catch {
		return []
	}
}

function extractRoutePubSubjects(head: string, prefix: string, maxSubjects = 8): string[] {
	try {
		return extractNatsCmdSubjects(head, 'PUB', maxSubjects).filter((subj) => subj.startsWith(prefix))
	} catch {
		return []
	}
}

export function installWsNatsProxy(server: HttpServer) {
	const path = (process.env['WS_NATS_PATH'] || '/nats').trim() || '/nats'
	const upstream = parseNatsUrl(process.env['NATS_URL'] || 'nats://nats:4222')
	const verbose = (process.env['WS_NATS_PROXY_VERBOSE'] || '0') === '1'
	const pingTrace = (process.env['WS_NATS_PROXY_PING_TRACE'] || '0') === '1'
	const wiretap = (process.env['WS_NATS_PROXY_WIRETAP'] || '0') === '1'
	const keepaliveEnabled = String(process.env['WS_NATS_PROXY_KEEPALIVE_ENABLE'] || '1') === '1'
	let keepalivePongWarnMs = 5_000
	try {
		keepalivePongWarnMs = Math.max(250, Number(process.env['WS_NATS_PROXY_KEEPALIVE_PONG_WARN_MS'] || '5000'))
	} catch {}
	const traceHttpRoute =
		(process.env['WS_NATS_PROXY_TRACE_HTTP_ROUTE'] || '0') === '1' ||
		(process.env['ROUTE_PROXY_TRACE'] || '0') === '1' ||
		(process.env['ROUTE_PROXY_HTTP_TRACE'] || '0') === '1' ||
		(process.env['ROUTE_PROXY_WS_TRACE'] || '0') === '1'
	// Workaround for flaky upstream PONG delivery: some environments drop NATS PONG after a few client PINGs
	// (breaking `flush()` and keepalives). When enabled, the proxy terminates client PINGs by immediately
	// replying with PONG and not forwarding the PING upstream.
	//
	// Important: this breaks the normal NATS `flush()` / PING-PONG end-to-end barrier semantics, because the
	// client can observe a local proxy PONG before the upstream NATS server has actually processed prior PUB/SUB.
	// Keep this OFF by default and enable only as an explicit workaround.
	const terminateClientPing = String(process.env['WS_NATS_PROXY_TERMINATE_CLIENT_PING'] || '0') === '1'
	// The proxy already answers upstream NATS `PING`s itself. Forwarding standalone client `PONG`s upstream as well
	// creates duplicate `PONG`s on the upstream TCP socket, which is the strongest remaining hypothesis for
	// upstream-side disconnects after ~40-60s. Strip only standalone `PONG` frames; mixed payloads still pass through.
	const stripClientPong = String(process.env['WS_NATS_PROXY_STRIP_CLIENT_PONG'] || '1') !== '0'
	// WARNING: attaching a `readable` listener to the underlying socket switches it into paused mode,
	// which can interfere with `ws` frame consumption. Keep this diagnostics path opt-in only.
	const socketReadableDiag = (process.env['WS_NATS_PROXY_SOCKET_READABLE_DIAG'] || '0') === '1'
	const activeHubSockets = new Map<
		string,
		Map<
			string,
			{
				ws: any
				close: (code?: number, reason?: string) => void
			}
		>
	>()
	let wiretapEveryMs = 1000
	try {
		wiretapEveryMs = Math.max(0, Number(process.env['WS_NATS_PROXY_WIRETAP_EVERY_MS'] || '1000'))
	} catch {}
	const wsPingEnabled = (process.env['WS_NATS_PROXY_WS_PING'] || '0') === '1'
	log().info({ path, upstream: { host: upstream.host, port: upstream.port } }, 'install ws->nats proxy')
	if (terminateClientPing) {
		log().warn(
			{ path },
			'ws-nats-proxy: WS_NATS_PROXY_TERMINATE_CLIENT_PING=1 enabled; this breaks end-to-end NATS flush semantics'
		)
	}

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
			try {
				const rawUrl = String(req?.url || '')
				const u = new URL(rawUrl, 'https://x')
				const q = u.searchParams.get('adaos_conn') || u.searchParams.get('conn') || u.searchParams.get('tag')
				if (typeof q === 'string' && q.trim()) return q.trim()
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
		let wsClosed = false
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
		let proxySentPongToClient = 0
		let clientSentPong = 0
		let clientSentPing = 0
		let upstreamSentPong = 0
		const openedAt = Date.now()
		let upstreamSock: net.Socket | null = null
		let wsPingTimer: NodeJS.Timeout | null = null
		let natsKeepaliveTimer: NodeJS.Timeout | null = null
		let upstreamNatsPingTimer: NodeJS.Timeout | null = null
		let wsPingsSent = 0
		let wsPongsReceived = 0
		let lastWsPongAt: number | null = null
		let natsKeepalivesSent = 0
		let upstreamNatsPingsSent = 0
		let keepaliveAwaitingPongSince: number | null = null
		let keepaliveAwaitingSocketDataSince: number | null = null
		let keepaliveAwaitingSocketReadableSince: number | null = null
		let lastClientMsgAt: number | null = null
		let lastClientSocketBytesRead: number | null = null
		let lastClientSocketBytesWritten: number | null = null
		let lastSocketDataAt: number | null = null
		let lastSocketReadableAt: number | null = null
		let lastSocketPauseAt: number | null = null
		let lastSocketResumeAt: number | null = null
		let socketDataEvents = 0
		let socketReadableEvents = 0
		let socketLastDataLen: number | null = null
		let lastClientWiretapAt = 0
		let lastUpstreamWiretapAt = 0
		let upstreamConnecting = false
		const upstreamPendingWrites: Buffer[] = []

		function closeBoth(code?: number, reason?: string) {
			try {
				ws.close(code || 1000, reason)
			} catch {}
			try {
				if (upstreamNatsPingTimer) clearInterval(upstreamNatsPingTimer)
			} catch {}
			upstreamNatsPingTimer = null
			try {
				upstreamSock?.destroy()
			} catch {}
		}

		function writeUpstream(
			buf: Buffer,
			why: string,
			traceRoute:
				| {
						subjects: string[]
						httpSubjects: string[]
				  }
				| undefined = undefined
		) {
			if (!buf.length) return
			if (!upstreamSock || (upstreamSock as any).destroyed) {
				if (traceRoute?.subjects?.length) {
					try {
						log().info(
							{
								conn: connId,
								tag: connTag,
								hub_id: hubIdForLog,
								len: buf.length,
								subjects: traceRoute.subjects,
								httpSubjects: traceRoute.httpSubjects,
								why,
							},
							'nats route upstream write missing'
						)
					} catch {}
				}
				logSummary('upstream missing while writing', { why })
				closeBoth(1011, 'upstream_missing')
				return
			}
			// If the upstream connection is still in-flight, queue writes until `connect`.
			// Node will also buffer writes, but keeping an explicit queue avoids subtle races
			// with repeated `connectUpstream()` calls and makes diagnostics clearer.
			if (!connected) {
				upstreamPendingWrites.push(buf)
				if (traceRoute?.subjects?.length) {
					try {
						log().info(
							{
								conn: connId,
								tag: connTag,
								hub_id: hubIdForLog,
								len: buf.length,
								subjects: traceRoute.subjects,
								httpSubjects: traceRoute.httpSubjects,
								why,
								queueLen: upstreamPendingWrites.length,
							},
							'nats route upstream write queued'
						)
					} catch {}
				}
				return
			}
			try {
				if (traceRoute?.subjects?.length) {
					try {
						log().info(
							{
								conn: connId,
								tag: connTag,
								hub_id: hubIdForLog,
								len: buf.length,
								subjects: traceRoute.subjects,
								httpSubjects: traceRoute.httpSubjects,
								why,
								writableLength: upstreamSock.writableLength,
							},
							'nats route upstream write start'
						)
					} catch {}
				}
				const ok = upstreamSock.write(buf)
				if (traceRoute?.subjects?.length) {
					try {
						log().info(
							{
								conn: connId,
								tag: connTag,
								hub_id: hubIdForLog,
								len: buf.length,
								subjects: traceRoute.subjects,
								httpSubjects: traceRoute.httpSubjects,
								why,
								ok,
								writableLength: upstreamSock.writableLength,
							},
							'nats route upstream write done'
						)
					} catch {}
				}
				if (ok === false) logSummary('upstream backpressure', { writableLength: upstreamSock.writableLength, why })
			} catch (e) {
				if (traceRoute?.subjects?.length) {
					try {
						log().info(
							{
								conn: connId,
								tag: connTag,
								hub_id: hubIdForLog,
								len: buf.length,
								subjects: traceRoute.subjects,
								httpSubjects: traceRoute.httpSubjects,
								why,
								err: String(e),
							},
							'nats route upstream write failed'
						)
					} catch {}
				}
				logSummary('upstream write failed', { err: String(e), why })
				closeBoth(1011, 'upstream_write_failed')
			}
		}

		function isWsOpen(): boolean {
			if (wsClosed) return false
			try {
				return ws.readyState === 1
			} catch {
				return false
			}
		}

		function getWsSocketDiag(extra?: Record<string, unknown>) {
			try {
				const sock: any = (ws as any)?._socket
				const socketBytesRead = sock ? Number(sock.bytesRead || 0) : null
				const socketBytesWritten = sock ? Number(sock.bytesWritten || 0) : null
				return {
					socketDestroyed: sock ? Boolean(sock.destroyed) : null,
					socketHadError: sock ? Boolean(sock.errored) : null,
					socketIsPaused: sock && typeof sock.isPaused === 'function' ? Boolean(sock.isPaused()) : null,
					socketReadableFlowing:
						sock && Object.prototype.hasOwnProperty.call(sock, 'readableFlowing')
							? ((sock as any).readableFlowing ?? null)
							: null,
					socketReadableLength:
						sock && typeof (sock as any).readableLength !== 'undefined'
							? Number((sock as any).readableLength || 0)
							: null,
					socketBytesRead,
					socketBytesWritten,
					socketBytesReadSinceLastClientMsg:
						socketBytesRead !== null && lastClientSocketBytesRead !== null
							? socketBytesRead - lastClientSocketBytesRead
							: null,
					socketBytesWrittenSinceLastClientMsg:
						socketBytesWritten !== null && lastClientSocketBytesWritten !== null
							? socketBytesWritten - lastClientSocketBytesWritten
							: null,
					lastSocketDataAgo_s: lastSocketDataAt ? (Date.now() - lastSocketDataAt) / 1000 : null,
					lastSocketReadableAgo_s: lastSocketReadableAt ? (Date.now() - lastSocketReadableAt) / 1000 : null,
					lastSocketPauseAgo_s: lastSocketPauseAt ? (Date.now() - lastSocketPauseAt) / 1000 : null,
					lastSocketResumeAgo_s: lastSocketResumeAt ? (Date.now() - lastSocketResumeAt) / 1000 : null,
					socketDataEvents,
					socketReadableEvents,
					socketLastDataLen,
					lastClientMsgAgo_s: lastClientMsgAt ? (Date.now() - lastClientMsgAt) / 1000 : null,
					remote: sock?.remoteAddress ? String(sock.remoteAddress) + ':' + String(sock.remotePort || '') : null,
					local: sock?.localAddress ? String(sock.localAddress) + ':' + String(sock.localPort || '') : null,
					...(extra || {}),
				}
			} catch {
				return {
					lastClientMsgAgo_s: lastClientMsgAt ? (Date.now() - lastClientMsgAt) / 1000 : null,
					...(extra || {}),
				}
			}
		}

		function getWsReceiverDiag() {
			try {
				const receiver: any = (ws as any)?._receiver
				return {
					receiverState:
						receiver && Object.prototype.hasOwnProperty.call(receiver, '_state') ? Number(receiver._state) : null,
					receiverOpcode:
						receiver && Object.prototype.hasOwnProperty.call(receiver, '_opcode') ? Number(receiver._opcode) : null,
					receiverBufferedBytes:
						receiver && Object.prototype.hasOwnProperty.call(receiver, '_bufferedBytes')
							? Number(receiver._bufferedBytes || 0)
							: null,
					receiverPayloadLength:
						receiver && Object.prototype.hasOwnProperty.call(receiver, '_payloadLength')
							? Number(receiver._payloadLength || 0)
							: null,
					receiverFragmented:
						receiver && Object.prototype.hasOwnProperty.call(receiver, '_fragmented')
							? Number(receiver._fragmented || 0)
							: null,
					receiverMasked:
						receiver && Object.prototype.hasOwnProperty.call(receiver, '_masked') ? Boolean(receiver._masked) : null,
					receiverCompressed:
						receiver && Object.prototype.hasOwnProperty.call(receiver, '_compressed')
							? Boolean(receiver._compressed)
							: null,
					receiverNeedDrain:
						receiver?._writableState && Object.prototype.hasOwnProperty.call(receiver._writableState, 'needDrain')
							? Boolean(receiver._writableState.needDrain)
							: null,
					receiverWritableLength:
						receiver?._writableState && Object.prototype.hasOwnProperty.call(receiver._writableState, 'length')
							? Number(receiver._writableState.length || 0)
							: null,
				}
			} catch {
				return {}
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
				proxySentPongToClient,
				clientSentPong,
				clientSentPing,
				upstreamSentPong,
				upstreamNatsPingsSent,
				wsPingsSent,
				wsPongsReceived,
				lastWsPongAgo_s: lastWsPongAt ? (Date.now() - lastWsPongAt) / 1000 : null,
				lastClientMsgAgo_s: lastClientMsgAt ? (Date.now() - lastClientMsgAt) / 1000 : null,
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
				// Short-lived "clean" closes are still suspicious for hub tunnels (they should be long-lived).
				// Keep these visible even when verbose logging is off.
				try {
					const isHub = typeof (base as any).hub_id === 'string' && Boolean((base as any).hub_id)
					const hs = Boolean((base as any).handshaked)
					const up = typeof (base as any).uptime_s === 'number' ? Number((base as any).uptime_s) : null
					if (isHub && hs && up !== null && up >= 0 && up < 180) {
						log().warn(base, event)
						return
					}
				} catch {}
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
				sock.on('data', (chunk: any) => {
					try {
						lastSocketDataAt = Date.now()
						socketDataEvents += 1
						const len = Buffer.isBuffer(chunk)
							? chunk.length
							: chunk && typeof chunk.length === 'number'
								? Number(chunk.length)
								: null
						socketLastDataLen = len
						if (keepaliveAwaitingSocketDataSince) {
							log().warn(
								{
									conn: connId,
									tag: connTag,
									hub_id: hubIdForLog,
									waitMs: Date.now() - keepaliveAwaitingSocketDataSince,
									dataLen: len,
									...getWsSocketDiag(),
									...getWsReceiverDiag(),
								},
								'ws socket data after keepalive'
							)
							keepaliveAwaitingSocketDataSince = null
						}
					} catch {}
				})
				if (socketReadableDiag) {
					sock.on('readable', () => {
						try {
							lastSocketReadableAt = Date.now()
							socketReadableEvents += 1
							if (keepaliveAwaitingSocketReadableSince) {
								log().warn(
									{
										conn: connId,
										tag: connTag,
										hub_id: hubIdForLog,
										waitMs: Date.now() - keepaliveAwaitingSocketReadableSince,
										...getWsSocketDiag(),
										...getWsReceiverDiag(),
									},
									'ws socket readable after keepalive'
								)
								keepaliveAwaitingSocketReadableSince = null
							}
						} catch {}
					})
				}
				sock.on('pause', () => {
					try {
						lastSocketPauseAt = Date.now()
						logSummary('ws socket pause', { ...getWsSocketDiag(), ...getWsReceiverDiag() })
					} catch {}
				})
				sock.on('resume', () => {
					try {
						lastSocketResumeAt = Date.now()
						logSummary('ws socket resume', { ...getWsSocketDiag(), ...getWsReceiverDiag() })
					} catch {}
				})
				sock.on('end', () => logSummary('ws socket end', { ...getWsSocketDiag(), ...getWsReceiverDiag() }))
				sock.on('close', (hadErr: any) =>
					logSummary('ws socket close', { ...getWsSocketDiag({ hadError: Boolean(hadErr) }), ...getWsReceiverDiag() })
				)
				sock.on('timeout', () => logSummary('ws socket timeout', { ...getWsSocketDiag(), ...getWsReceiverDiag() }))
				sock.on('error', (e: any) =>
					logSummary('ws socket error', { ...getWsSocketDiag({ err: String(e) }), ...getWsReceiverDiag() })
				)
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
			if (!keepaliveEnabled) return
			if (natsKeepaliveTimer) clearInterval(natsKeepaliveTimer)
			const requireHandshake = String(process.env.WS_NATS_PROXY_KEEPALIVE_REQUIRE_HANDSHAKE || '1') !== '0'
			let warnedNoHandshake = false
			// Many NATs/firewalls time out idle outbound mappings. WS control frames may be ignored by
			// intermediaries, so we send a tiny NATS protocol keepalive as *data* to the client.
			// The hub's nats client will respond with `PONG`, which creates outbound traffic hub->root.
			natsKeepaliveTimer = setInterval(() => {
				try {
					if (requireHandshake && !handshaked) {
						if (!warnedNoHandshake && (verbose || pingTrace)) {
							warnedNoHandshake = true
							log().warn({ conn: connId, tag: connTag, hub_id: hubIdForLog }, 'nats keepalive skipped: not handshaked yet')
						}
						return
					}
					if (ws.readyState !== 1) return
					const pingSentAt = Date.now()
					ws.send(NATS_PING, { binary: true }, (err?: Error) => {
						try {
							if (err) {
								log().warn({ conn: connId, tag: connTag, hub_id: hubIdForLog, handshaked, err: String(err) }, 'nats keepalive send failed')
								return
							}
							if (pingTrace) {
								log().info(
									{
										conn: connId,
										tag: connTag,
										hub_id: hubIdForLog,
										handshaked,
										sendMs: Date.now() - pingSentAt,
									},
									'nats ping (keepalive -> client) sent'
								)
							}
						} catch {}
					})
					natsKeepalivesSent += 1
					keepaliveAwaitingPongSince = pingSentAt
					keepaliveAwaitingSocketDataSince = pingSentAt
					if (socketReadableDiag) keepaliveAwaitingSocketReadableSince = pingSentAt
					if (pingTrace) log().info({ conn: connId, tag: connTag, hub_id: hubIdForLog, handshaked }, 'nats ping (keepalive -> client)')
					setTimeout(() => {
						try {
							if (keepaliveAwaitingPongSince !== pingSentAt) return
							if (ws.readyState !== 1) return
							log().warn(
								{
									conn: connId,
									tag: connTag,
									hub_id: hubIdForLog,
									handshaked,
									waitMs: Date.now() - pingSentAt,
									lastClientPongAgo_s: lastClientPongAt ? (Date.now() - lastClientPongAt) / 1000 : null,
									lastClientPingAgo_s: lastClientPingAt ? (Date.now() - lastClientPingAt) / 1000 : null,
									lastUpstreamPingAgo_s: lastUpstreamPingAt ? (Date.now() - lastUpstreamPingAt) / 1000 : null,
									lastUpstreamPongAgo_s: lastUpstreamPongAt ? (Date.now() - lastUpstreamPongAt) / 1000 : null,
									natsKeepalivesSent,
									wsReadyState: ws.readyState,
									wsBufferedAmount: Number((ws as any)?.bufferedAmount || 0),
									...getWsSocketDiag(),
									...getWsReceiverDiag(),
								},
								'nats keepalive pong missing'
							)
						} catch {}
					}, keepalivePongWarnMs)
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

		function armUpstreamNatsKeepalive() {
			try {
				if (upstreamNatsPingTimer) clearInterval(upstreamNatsPingTimer)
			} catch {}
			upstreamNatsPingTimer = null
			const pingMs = Math.max(
				5000,
				Number.parseInt(process.env.WS_NATS_PROXY_UPSTREAM_NATS_PING_MS || '20000', 10) || 20000,
			)
			const requireHandshake = String(process.env.WS_NATS_PROXY_UPSTREAM_NATS_PING_REQUIRE_HANDSHAKE || '1') !== '0'
			upstreamNatsPingTimer = setInterval(() => {
				try {
					if (requireHandshake && !handshaked) return
					const sock = upstreamSock
					if (!sock || (sock as any).destroyed || !connected) return
					const ok = sock.write(NATS_PING)
					upstreamNatsPingsSent += 1
					if (ok === false) {
						logSummary('upstream keepalive backpressure', { writableLength: sock.writableLength, upstreamNatsPingsSent })
					} else if (pingTrace) {
						log().info({ conn: connId, tag: connTag, hub_id: hubIdForLog, upstreamNatsPingsSent }, 'nats ping (proxy -> upstream)')
					}
				} catch (e) {
					logSummary('upstream keepalive write failed', { err: String(e), upstreamNatsPingsSent })
				}
			}, pingMs)
		}

		function disarmUpstreamNatsKeepalive() {
			try {
				if (upstreamNatsPingTimer) clearInterval(upstreamNatsPingTimer)
			} catch {}
			upstreamNatsPingTimer = null
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
				let routeToHubSubjects: string[] = []
				let httpRouteToHubSubjects: string[] = []
				if (traceHttpRoute) {
					try {
						const head = chunk.subarray(0, Math.min(chunk.length, 8192)).toString('utf8')
						routeToHubSubjects = extractRouteMsgSubjects(head, 'route.to_hub.')
						httpRouteToHubSubjects = routeToHubSubjects.filter((subj) => subj.includes('--http--'))
						if (routeToHubSubjects.length > 0) {
							log().info(
								{
									conn: connId,
									tag: connTag,
									hub_id: hubIdForLog,
									len: chunk.length,
									subjects: routeToHubSubjects,
									httpSubjects: httpRouteToHubSubjects,
									wsReadyState: ws.readyState,
									wsBufferedAmount: Number((ws as any)?.bufferedAmount || 0),
								},
								'nats route chunk (upstream->proxy)'
							)
						}
					} catch {}
				}
				if (wiretap) {
					try {
						const now = Date.now()
						if (wiretapEveryMs === 0 || now - lastUpstreamWiretapAt >= wiretapEveryMs) {
							lastUpstreamWiretapAt = now
							try {
								const head = chunk.subarray(0, Math.min(chunk.length, 2048)).toString('utf8')
								const counts = {
									ping: (head.match(/\bPING\r\n/g) || []).length,
									pong: (head.match(/\bPONG\r\n/g) || []).length,
									msg: (head.match(/\bMSG /g) || []).length,
									info: (head.match(/\bINFO /g) || []).length,
									err: head.includes('-ERR') ? 1 : 0,
								}
								log().info(
									{ conn: connId, tag: connTag, hub_id: hubIdForLog, len: chunk.length, ...counts },
									'nats wiretap (upstream->proxy)',
								)
							} catch {}
						}
					} catch {}
				}
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
					const routeSendStartedAt = routeToHubSubjects.length > 0 ? Date.now() : 0
					if (routeToHubSubjects.length > 0) {
						try {
							log().info(
								{
									conn: connId,
									tag: connTag,
									hub_id: hubIdForLog,
									len: chunk.length,
									subjects: routeToHubSubjects,
									httpSubjects: httpRouteToHubSubjects,
									wsReadyState: ws.readyState,
									wsBufferedAmount: Number((ws as any)?.bufferedAmount || 0),
								},
								'nats route downstream send start'
							)
						} catch {}
					}
					ws.send(chunk, { binary: true }, (err: any) => {
						if (routeToHubSubjects.length > 0) {
							try {
								const callbackMs = Math.max(0, Date.now() - routeSendStartedAt)
								log().info(
									{
										conn: connId,
										tag: connTag,
										hub_id: hubIdForLog,
										len: chunk.length,
										subjects: routeToHubSubjects,
										httpSubjects: httpRouteToHubSubjects,
										callbackMs,
										wsReadyState: ws.readyState,
										wsBufferedAmount: Number((ws as any)?.bufferedAmount || 0),
										err: err ? String(err) : undefined,
									},
									err ? 'nats route downstream send failed' : 'nats route downstream send done'
								)
							} catch {}
						}
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
				disarmUpstreamNatsKeepalive()
				logSummary('upstream close', { hadError: Boolean(hadError) })
				try {
					ws_nats_proxy_upstream_close_total.labels(hadError ? '1' : '0').inc()
				} catch {}
				closeBoth(1000, 'upstream_close')
			})
			sock.on('error', (err) => {
				upstreamConnecting = false
				connected = false
				disarmUpstreamNatsKeepalive()
				logSummary('upstream error', { err: String(err) })
				closeBoth(1011, 'upstream_error')
			})
		}

		function tryProcessHandshake(): boolean {
			if (handshaked || authInFlight) return false
			if (!isWsOpen()) {
				clientBuf = Buffer.alloc(0)
				preHandshakeQueue = []
				logSummary('skip handshake: ws not open', { wsReadyState: ws.readyState })
				return true
			}
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

			const authVerifyStartedAt = Date.now()
			const slowAuthTimer = setTimeout(() => {
				try {
					log().warn(
						{
							from: rip,
							conn: connId,
							tag: connTag,
							hub_id: hubId,
							user: userRaw,
							pendingMs: Date.now() - authVerifyStartedAt,
							restLen: rest.length,
							preHandshakeQueueLen: preHandshakeQueue.length,
							wsReadyState: ws.readyState,
							upstreamConnected: connected,
							upstreamConnecting,
						},
						'auth verify pending'
					)
				} catch {}
			}, AUTH_VERIFY_PENDING_MS)
			try {
				log().info(
					{
						from: rip,
						conn: connId,
						tag: connTag,
						hub_id: hubId,
						user: userRaw,
						restLen: rest.length,
						preHandshakeQueueLen: preHandshakeQueue.length,
					},
					'auth verify start'
				)
			} catch {}

			verifyHubToken(hubId, passRaw)
				.then((ok) => {
					clearTimeout(slowAuthTimer)
					const verifyMs = Date.now() - authVerifyStartedAt
					if (!isWsOpen()) {
						authInFlight = false
						preHandshakeQueue = []
						clientBuf = Buffer.alloc(0)
						logSummary('skip auth result: ws not open', {
							hub_id: hubId,
							verifyMs,
							wsReadyState: ws.readyState,
						})
						return
					}
					if (!ok) {
						log().warn(
							{
								from: rip,
								conn: connId,
								tag: connTag,
								hub_id: hubId,
								user: userRaw,
								pass: mask(passRaw),
								verifyMs,
							},
							'auth failed'
						)
						closeBoth(1008, 'auth_failed')
						authInFlight = false
						return
					}
					try {
						log().info(
							{
								from: rip,
								conn: connId,
								tag: connTag,
								hub_id: hubId,
								user: userRaw,
								verifyMs,
							},
							'auth verify ok'
						)
					} catch {}

					hubIdForLog = hubId
					try {
						const peers = activeHubSockets.get(hubId)
						const next = peers ? new Map(peers) : new Map()
						const supersededConnIds: string[] = []
						if (peers?.size) {
							for (const [peerConnId, peer] of peers.entries()) {
								if (!peer || peer.ws === ws) continue
								supersededConnIds.push(peerConnId)
								next.delete(peerConnId)
								try {
									log().warn(
										{
											hub_id: hubId,
											conn: connId,
											tag: connTag,
											superseded_conn: peerConnId,
										},
										'closing superseded hub ws-nats connection',
									)
								} catch {}
								try {
									peer.close(1001, 'superseded')
								} catch {}
							}
						}
						next.set(connId, { ws, close: closeBoth })
						activeHubSockets.set(hubId, next)
						try {
							log().info(
								{
									hub_id: hubId,
									conn: connId,
									tag: connTag,
									peerCount: next.size,
									peers: Array.from(next.keys()),
									superseded: supersededConnIds,
								},
								'hub ws-nats auth ok'
							)
						} catch {}
					} catch {}

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
					try {
						if (!isWsOpen()) {
							authInFlight = false
							preHandshakeQueue = []
							clientBuf = Buffer.alloc(0)
							logSummary('skip auth rewrite: ws not open', {
								hub_id: hubId,
								verifyMs,
								wsReadyState: ws.readyState,
							})
							return
						}
						const queued = preHandshakeQueue.length ? Buffer.concat(preHandshakeQueue) : Buffer.alloc(0)
						const queuedBytes = queued.length
						const totalMs = Date.now() - authVerifyStartedAt
						try {
							log().info(
								{
									from: rip,
									conn: connId,
									tag: connTag,
									hub_id: hubId,
									verifyMs,
									totalMs,
									rewrittenLen: rewritten.length,
									restLen: rest.length,
									queuedBytes,
									upstreamConnected: connected,
									upstreamConnecting,
								},
								'auth rewrite start'
							)
						} catch {}
						writeUpstream(rewritten, 'connect')
						// Send any bytes that arrived after CONNECT while auth was in flight.
						preHandshakeQueue = []
						const tail = queued.length ? Buffer.concat([rest, queued]) : rest
						if (tail.length) {
							writeUpstream(tail, 'connect_tail')
						}
						clientBuf = Buffer.alloc(0)
						handshaked = true
						armUpstreamNatsKeepalive()
						authInFlight = false
						log().info(
							{
								from: rip,
								conn: connId,
								tag: connTag,
								hub_id: hubId,
								verifyMs,
								totalMs: Date.now() - authVerifyStartedAt,
								tailLen: tail.length,
								queuedBytes,
							},
							'auth ok'
						)
					} catch (e) {
						log().warn(
							{
								conn: connId,
								tag: connTag,
								hub_id: hubId,
								verifyMs,
								totalMs: Date.now() - authVerifyStartedAt,
								err: String(e),
							},
							'write upstream failed'
						)
						authInFlight = false
						closeBoth(1011, 'upstream_write_failed')
					}
				})
				.catch((e) => {
					clearTimeout(slowAuthTimer)
					log().error(
						{
							from: rip,
							conn: connId,
							tag: connTag,
							hub_id: hubId,
							tookMs: Date.now() - authVerifyStartedAt,
							err: String(e),
						},
						'auth error'
					)
					authInFlight = false
					closeBoth(1011, 'auth_error')
				})

			return true
		}

		ws.on('message', (data: any) => {
			let buf = toBuffer(data)
			try {
				lastClientMsgAt = Date.now()
				const sock: any = (ws as any)?._socket
				lastClientSocketBytesRead = sock ? Number(sock.bytesRead || 0) : null
				lastClientSocketBytesWritten = sock ? Number(sock.bytesWritten || 0) : null
				keepaliveAwaitingSocketDataSince = null
				keepaliveAwaitingSocketReadableSince = null
				// Any client data proves liveness; clear pending keepalive wait to avoid false timeouts
				if (keepaliveAwaitingPongSince !== null) {
					keepaliveAwaitingPongSince = null
				}
			} catch {}
			if (authInFlight && !handshaked) {
				// Prevent double CONNECT parsing when the client keeps sending data while auth is in-flight.
				preHandshakeQueue.push(buf)
				return
			}
			if (handshaked) {
				// Terminate standalone NATS PING from the client. This makes hub flush/keepalive resilient even if
				// upstream stops responding with PONG after a few PINGs.
				if (terminateClientPing && buf.length === NATS_PING.length && buf.equals(NATS_PING)) {
					try {
						bytesUp += buf.length
						lastClientPingAt = Date.now()
						clientSentPing += 1
						ws.send(NATS_PONG, { binary: true })
						proxySentPongToClient += 1
						if (pingTrace) log().info({ conn: connId, tag: connTag, hub_id: hubIdForLog }, 'nats ping (client->proxy) -> pong (proxy)')
					} catch (e) {
						logSummary('proxy pong to client failed', { err: String(e) })
						closeBoth(1011, 'client_pong_failed')
					}
					return
				}
				try {
					bytesUp += buf.length
					let routeToBrowserSubjects: string[] = []
					let httpRouteToBrowserSubjects: string[] = []
					if (traceHttpRoute) {
						try {
							const head = buf.subarray(0, Math.min(buf.length, 2048)).toString('utf8')
							routeToBrowserSubjects = extractRoutePubSubjects(head, 'route.to_browser.')
							httpRouteToBrowserSubjects = routeToBrowserSubjects.filter((subj) => subj.includes('--http--'))
							if (routeToBrowserSubjects.length > 0) {
								log().info(
									{
										conn: connId,
										tag: connTag,
										hub_id: hubIdForLog,
										len: buf.length,
										subjects: routeToBrowserSubjects,
										httpSubjects: httpRouteToBrowserSubjects,
										upstreamConnected: connected,
										upstreamWritableLength: upstreamSock?.writableLength ?? null,
									},
									'nats route chunk (client->proxy)'
								)
							}
							if (httpRouteToBrowserSubjects.length > 0) {
								log().info(
									{
										conn: connId,
										tag: connTag,
										hub_id: hubIdForLog,
										len: buf.length,
										subjects: httpRouteToBrowserSubjects,
									},
									'nats http route (client->proxy)'
								)
							}
						} catch {}
					}
					if (wiretap) {
						const now = Date.now()
						if (wiretapEveryMs === 0 || now - lastClientWiretapAt >= wiretapEveryMs) {
							lastClientWiretapAt = now
							try {
								// Do NOT log raw payloads (may contain Telegram/user content).
								// Instead, log coarse protocol markers to answer "client sends nothing?" questions.
								const head = buf.subarray(0, Math.min(buf.length, 2048)).toString('utf8')
								const counts = {
									connect: head.includes('CONNECT ') ? 1 : 0,
									ping: (head.match(/\bPING\r\n/g) || []).length,
									pong: (head.match(/\bPONG\r\n/g) || []).length,
									sub: (head.match(/\bSUB /g) || []).length,
									unsub: (head.match(/\bUNSUB /g) || []).length,
									pub: (head.match(/\bPUB /g) || []).length,
									msg: (head.match(/\bMSG /g) || []).length,
									info: (head.match(/\bINFO /g) || []).length,
									err: head.includes('-ERR') ? 1 : 0,
								}
								log().info({ conn: connId, tag: connTag, hub_id: hubIdForLog, len: buf.length, ...counts }, 'nats wiretap (client->proxy)')
							} catch {}
						}
					}
					const scanPong = hasMarkerWithTail(clientTail, buf, NATS_PONG)
					clientTail = scanPong.tail
					if (scanPong.hit) {
						lastClientPongAt = Date.now()
						clientSentPong += 1
						keepaliveAwaitingPongSince = null
						keepaliveAwaitingSocketDataSince = null
						keepaliveAwaitingSocketReadableSince = null
						if (pingTrace) log().info({ conn: connId, hub_id: hubIdForLog }, 'nats pong (from client)')
					}
					// Track client PINGs too (nats-py sends these as keepalive).
					if (buf.includes(NATS_PING)) {
						lastClientPingAt = Date.now()
						clientSentPing += 1
						if (pingTrace) log().info({ conn: connId, hub_id: hubIdForLog }, 'nats ping (from client)')
					}
				} catch {}
				if (stripClientPong && buf.length === NATS_PONG.length && buf.equals(NATS_PONG)) {
					if (pingTrace) {
						log().info({ conn: connId, tag: connTag, hub_id: hubIdForLog }, 'nats pong (client->proxy) stripped upstream')
					}
					return
				}
				let traceRoute:
					| {
							subjects: string[]
							httpSubjects: string[]
					  }
					| undefined
				try {
					if (traceHttpRoute) {
						const head = buf.subarray(0, Math.min(buf.length, 2048)).toString('utf8')
						const subjects = extractRoutePubSubjects(head, 'route.to_browser.')
						if (subjects.length > 0) {
							traceRoute = {
								subjects,
								httpSubjects: subjects.filter((subj) => subj.includes('--http--')),
							}
						}
					}
				} catch {}
				writeUpstream(buf, 'client_data', traceRoute)
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
			wsClosed = true
			logSummary('ws error', { err: String(err) })
			closeBoth()
		})

		ws.on('close', (code: number, reasonBuf: any) => {
			wsClosed = true
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
			try {
				if (hubIdForLog) {
					const peers = activeHubSockets.get(hubIdForLog)
					if (peers) {
						peers.delete(connId)
						if (peers.size === 0) activeHubSockets.delete(hubIdForLog)
					}
				}
			} catch {}
			// Extra diagnostics for abnormal closes (1006 = no close frame).
			if (code === 1006) {
				try {
					logSummary('ws close 1006 diag', { ...getWsSocketDiag(), ...getWsReceiverDiag() })
				} catch {}
			}
			logSummary('conn close', { code, reason })
			try {
				ws_nats_proxy_conn_close_total.labels(String(code)).inc()
			} catch {}
			disarmWsPing()
			disarmNatsKeepalive()
			disarmUpstreamNatsKeepalive()
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
