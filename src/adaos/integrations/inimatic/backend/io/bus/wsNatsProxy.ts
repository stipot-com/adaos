import type { Server as HttpsServer } from 'https'
import net from 'node:net'
import pino from 'pino'
// dynamic import for 'ws' to avoid hard dependency during build
import { verifyHubToken } from '../../db/tg.repo.js'

const log = pino({ name: 'ws-nats-proxy' })

type DebugEvent = {
  ts: string
  from?: string
  event: string
  details?: any
}

const _recent: DebugEvent[] = []
function pushDbg(ev: Omit<DebugEvent, 'ts'>) {
  _recent.push({ ts: new Date().toISOString(), ...ev })
  if (_recent.length > 200) _recent.shift()
}

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

  let WebSocketServerCtor: any
  try {
    // eslint-disable-next-line no-new-func
    const mod: any = (new Function('m', 'return import(m)'))('ws')
    // handle promise-like dynamic import
    ;(Promise.resolve(mod).then((m: any) => {
      WebSocketServerCtor = m.WebSocketServer || m.Server
      if (!WebSocketServerCtor) throw new Error('ws package missing WebSocketServer export')
      const wss = new WebSocketServerCtor({
        server,
        path,
        perMessageDeflate: false,
        handleProtocols: (protocols: string[], _req: any) => {
          // Prefer NATS subprotocol when offered by client
          return protocols.includes('nats') ? 'nats' : (protocols[0] || undefined)
        },
      })

  wss.on('connection', (ws: any, req: any) => {
    const rip = (req.headers['x-forwarded-for'] as string) || req.socket.remoteAddress || ''
    log.info({ from: rip }, 'conn open')
    pushDbg({ from: rip, event: 'conn_open' })

        let connected = false
        let handshaked = false
        let clientBuf = Buffer.alloc(0)
        let upstreamSock: net.Socket | null = null
        let upstreamPingTimer: NodeJS.Timeout | null = null
        function armUpstreamPingWatch() {
          if (upstreamPingTimer) clearTimeout(upstreamPingTimer)
          upstreamPingTimer = setTimeout(() => {
            try { upstreamSock?.write(Buffer.from('PONG\r\n', 'utf8')) } catch {}
            upstreamPingTimer = null
          }, 1500)
        }
        function disarmUpstreamPingWatch() {
          if (upstreamPingTimer) { clearTimeout(upstreamPingTimer); upstreamPingTimer = null }
        }

        function closeBoth(code?: number, reason?: string) {
          try { ws.close(code || 1000, reason) } catch {}
          try { upstreamSock?.destroy() } catch {}
        }

        function connectUpstream() {
          if (connected) return
          upstreamSock = net.createConnection({ host: upstream.host, port: upstream.port })
          try { (upstreamSock as any).setNoDelay?.(true) } catch {}
          upstreamSock.on('connect', () => {
            connected = true
          })
      upstreamSock.on('data', (chunk) => {
        // Forward upstream INFO/PING/PONG as binary to satisfy clients expecting bytes (e.g., python nats ws)
        const txt = chunk.toString('utf8')
        pushDbg({ from: rip, event: 'upstream_data', details: { len: chunk.length, sample: txt.slice(0, 200) } })
        if (txt.startsWith('-ERR')) {
          log.warn({ from: rip, err: txt.trim() }, 'upstream -ERR')
          pushDbg({ from: rip, event: 'upstream_err', details: { line: txt.trim() } })
        }
        if (txt.includes('PING')) armUpstreamPingWatch()
        try { ws.send(chunk, { binary: true }) } catch {}
      })
          upstreamSock.on('error', (err) => {
            log.warn({ err: String(err) }, 'upstream error')
            closeBoth(1011, 'upstream_error')
          })
          upstreamSock.on('close', (hadError: any) => {
            log.info({ from: rip, hadError: !!hadError }, 'upstream close')
            pushDbg({ from: rip, event: 'upstream_close', details: { hadError: !!hadError } })
            closeBoth()
          })
        }

        function tryProcessHandshake(): boolean {
          const s = clientBuf.toString('utf8')
          // support both CRLF and LF
          let idx = s.indexOf('\r\n')
          let eolLen = 2
          if (idx === -1) {
            idx = s.indexOf('\n')
            eolLen = 1
          }
          if (idx === -1) return false
          const line = s.slice(0, idx)
          const rest = Buffer.from(s.slice(idx + eolLen), 'utf8')
          if (!line.startsWith('CONNECT ')) {
            log.warn({ from: rip, line }, 'unexpected first line (expected CONNECT)')
            pushDbg({ from: rip, event: 'unexpected_first_line', details: { line: line.slice(0, 200) } })
            closeBoth(1008, 'protocol_error')
            return true
          }
          const jsonRaw = line.slice('CONNECT '.length).trim()
          let obj: any
          try { obj = JSON.parse(jsonRaw) } catch (e) {
            log.warn({ from: rip, err: String(e) }, 'bad CONNECT json')
            pushDbg({ from: rip, event: 'bad_connect_json', details: { err: String(e) } })
            closeBoth(1008, 'bad_connect')
            return true
          }
          const userRaw = String(obj?.user || '')
          const passRaw = String(obj?.pass || '')
          const hubId = userRaw.startsWith('hub_') ? userRaw.slice(4) : userRaw

          verifyHubToken(hubId, passRaw)
            .then((ok) => {
              if (!ok) {
                log.warn({ from: rip, hub_id: hubId, user: userRaw, pass: mask(passRaw) }, 'auth failed')
                pushDbg({ from: rip, event: 'auth_failed', details: { hub_id: hubId, user: userRaw } })
                closeBoth(1008, 'auth_failed')
                return
              }
              // Rewrite credentials to backend NATS and remove conflicting auth fields
              const u: any = { ...obj }
              try { delete u.auth_token } catch {}
              try { delete u.jwt } catch {}
              try { delete u.nkey } catch {}
              try { delete u.sig } catch {}
              u.user = upstream.user
              u.pass = upstream.pass
              const rewritten = Buffer.from('CONNECT ' + JSON.stringify(u) + '\r\n', 'utf8')
              connectUpstream()
              setTimeout(() => {
                try {
                  pushDbg({ from: rip, event: 'rewrite_connect', details: { user: mask(upstream.user) } })
                  upstreamSock?.write(rewritten)
                  if (rest.length) upstreamSock?.write(rest)
                  clientBuf = Buffer.alloc(0)
                  handshaked = true
                  log.info({ from: rip, hub_id: hubId }, 'auth ok')
                  pushDbg({ from: rip, event: 'auth_ok', details: { hub_id: hubId } })
                } catch (e) {
                  log.warn({ err: String(e) }, 'write upstream failed')
                  pushDbg({ from: rip, event: 'upstream_write_failed', details: { err: String(e) } })
                  closeBoth(1011, 'upstream_write_failed')
                }
              }, 0)
            })
            .catch((e) => {
              log.error({ from: rip, err: String(e) }, 'auth error')
              pushDbg({ from: rip, event: 'auth_error', details: { err: String(e) } })
              closeBoth(1011, 'auth_error')
            })
          return true
        }

        ws.on('message', (data: any) => {
          if (handshaked) {
            // cancel upstream ping watchdog if client replied with PONG
            try {
              const s = typeof data === 'string' ? data : (data as Buffer).toString('utf8')
              if (s.includes('PONG')) disarmUpstreamPingWatch()
            } catch {}
            try { upstreamSock?.write(data as Buffer) } catch {}
            return
          }
          const buf = typeof data === 'string' ? Buffer.from(data) : (data as Buffer)
          const isStr = typeof data === 'string'
          pushDbg({ from: rip, event: 'client_frame', details: { type: isStr ? 'text' : 'binary', len: (isStr ? (data as string).length : (data as Buffer).length) } })
          clientBuf = Buffer.concat([clientBuf, buf])
          tryProcessHandshake()
        })

        ws.on('error', (err: any) => {
          log.warn({ from: rip, err: String(err) }, 'ws error')
          pushDbg({ from: rip, event: 'ws_error', details: { err: String(err) } })
          closeBoth()
        })
        ws.on('close', (code: number, reasonBuf: any) => {
          const reason = (() => {
            try { return typeof reasonBuf === 'string' ? reasonBuf : Buffer.isBuffer(reasonBuf) ? (reasonBuf as Buffer).toString('utf8') : '' } catch { return '' }
          })()
          log.info({ from: rip, code, reason }, 'conn close')
          pushDbg({ from: rip, event: 'conn_close', details: { code, reason } })
          try { upstreamSock?.destroy() } catch {}
        })

        connectUpstream()
      })

    }).catch((e: any) => {
      log.error({ err: String(e) }, 'ws module load failed')
    }))
  } catch (e) {
    log.error({ err: String(e) }, 'ws dynamic import failed')
  }
}

export function installWsNatsProxyDebugRoute(app: any) {
  try {
    app.get('/internal/debug/ws-nats-proxy', (_req: any, res: any) => {
      const expect = process.env['ADAOS_TOKEN'] || ''
      const header = String(_req?.header?.('X-AdaOS-Token') || '')
      if (!expect || header !== expect) {
        return res.status(401).json({ error: 'unauthorized' })
      }
      res.json({ recent: _recent.slice(-100) })
    })
  } catch (e) {
    log.warn({ err: String(e) }, 'failed to install ws-nats-proxy debug route')
  }
}
