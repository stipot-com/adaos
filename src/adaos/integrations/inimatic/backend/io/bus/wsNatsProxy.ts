import type { Server as HttpsServer } from 'https'
import net from 'node:net'
import pino from 'pino'
import { WebSocketServer } from 'ws'
import { verifyHubToken } from '../../db/tg.repo.js'

const log = pino({ name: 'ws-nats-proxy' })

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
		let upstreamSock: net.Socket | null = null
		let upstreamPingTimer: NodeJS.Timeout | null = null

		function armUpstreamPingWatch() {
			if (upstreamPingTimer) clearTimeout(upstreamPingTimer)
			upstreamPingTimer = setTimeout(() => {
				try {
					upstreamSock?.write(Buffer.from('PONG\r\n', 'utf8'))
				} catch {}
				upstreamPingTimer = null
			}, 1500)
		}

		function disarmUpstreamPingWatch() {
			if (upstreamPingTimer) {
				clearTimeout(upstreamPingTimer)
				upstreamPingTimer = null
			}
		}

		function closeBoth(code?: number, reason?: string) {
			try {
				ws.close(code || 1000, reason)
			} catch {}
			try {
				upstreamSock?.destroy()
			} catch {}
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
				const txt = chunk.toString('utf8')
				if (txt.includes('PING')) armUpstreamPingWatch()
				try {
					ws.send(chunk, { binary: true })
				} catch {}
			})
			upstreamSock.on('close', (hadError) => {
				log.info({ from: rip, hadError: Boolean(hadError) }, 'upstream close')
				closeBoth(1000, 'upstream_close')
			})
			upstreamSock.on('error', (err) => {
				log.warn({ from: rip, err: String(err) }, 'upstream error')
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
			if (handshaked) {
				try {
					const s = typeof data === 'string' ? data : (data as Buffer).toString('utf8')
					if (s.includes('PONG')) disarmUpstreamPingWatch()
				} catch {}
				try {
					upstreamSock?.write(data as Buffer)
				} catch {}
				return
			}
			const buf = typeof data === 'string' ? Buffer.from(data) : (data as Buffer)
			clientBuf = Buffer.concat([clientBuf, buf])
			tryProcessHandshake()
		})

		ws.on('error', (err: any) => {
			log.warn({ from: rip, err: String(err) }, 'ws error')
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
			log.info({ from: rip, code, reason }, 'conn close')
			try {
				upstreamSock?.destroy()
			} catch {}
		})

		connectUpstream()
	})
}
