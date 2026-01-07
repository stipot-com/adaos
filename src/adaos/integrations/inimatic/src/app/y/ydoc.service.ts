import { Injectable } from '@angular/core'
import * as Y from 'yjs'
import { IndexeddbPersistence } from 'y-indexeddb'
import { WebsocketProvider } from 'y-websocket'
import { AdaosClient } from '../core/adaos/adaos-client.service'

@Injectable({ providedIn: 'root' })
export class YDocService {
  public readonly doc = new Y.Doc()
  private db?: IndexeddbPersistence
  private provider?: WebsocketProvider
  private initialized = false
  private initPromise?: Promise<void>
  private readonly deviceId: string
  private currentWebspaceId = 'default'
  private readonly webspaceKey = 'adaos_webspace_id'
  private readonly hubIdKey = 'adaos_hub_id'
  private readonly sessionJwtKey = 'adaos_web_session_jwt'

  constructor(private adaos: AdaosClient) {
    this.deviceId = this.ensureDeviceId()
  }

  private ensureDeviceId(): string {
    const key = 'adaos_device_id'
    try {
      const existing = localStorage.getItem(key)
      if (existing) return existing
      const raw = (globalThis.crypto && (crypto as any).randomUUID?.()) || Math.random().toString(36).slice(2)
      const id = `dev_${raw}`
      localStorage.setItem(key, id)
      return id
    } catch {
      const raw = Math.random().toString(36).slice(2)
      return `dev_${raw}`
    }
  }

  getDeviceId(): string {
    return this.deviceId
  }

  private invalidateWebSession(): void {
    try {
      localStorage.removeItem(this.sessionJwtKey)
    } catch {}
    try {
      localStorage.removeItem(this.hubIdKey)
    } catch {}
    try {
      this.adaos.setAuthAdaosToken(null)
    } catch {}
  }

  private decodeJwtPayload(token: string): any | null {
    try {
      const parts = token.split('.')
      if (parts.length < 2) return null
      const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
      const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4)
      const json = atob(padded)
      return JSON.parse(json)
    } catch {
      return null
    }
  }

  private isJwtValid(token: string): boolean {
    const payload = this.decodeJwtPayload(token)
    const exp = typeof payload?.exp === 'number' ? payload.exp : undefined
    if (!exp) return false
    const now = Math.floor(Date.now() / 1000)
    return exp > now + 15
  }

  private pickHubIdFromJwt(token: string): string | null {
    const payload = this.decodeJwtPayload(token)
    const hubId = payload?.hub_id || payload?.subnet_id || payload?.owner_id
    return typeof hubId === 'string' && hubId.trim() ? hubId.trim() : null
  }

  async initFromHub(): Promise<void> {
    if (this.initialized) return
    if (this.initPromise) return this.initPromise
    this.initPromise = this.doInitFromHub()
      .then(() => {
        this.initialized = true
      })
      .finally(() => {
        // Allow retries after failures (e.g. hub offline until user logs in to use root-proxy).
        this.initPromise = undefined
      })
    return this.initPromise
  }

  private async doInitFromHub(): Promise<void> {
    // If we're re-initializing after a failure, make sure old provider is torn down.
    try {
      this.provider?.destroy()
    } catch {}
    this.provider = undefined

    const readSession = (): { hubId: string | null; sessionJwt: string | null } => {
      try {
        const sessionJwt = localStorage.getItem(this.sessionJwtKey)
        let hubId = localStorage.getItem(this.hubIdKey)
        // Heal missing hub id from the JWT itself (best-effort).
        if (!hubId && sessionJwt && sessionJwt.includes('.')) {
          hubId = this.pickHubIdFromJwt(sessionJwt)
          try {
            if (hubId) localStorage.setItem(this.hubIdKey, hubId)
          } catch {}
        }
        return { hubId, sessionJwt }
      } catch {
        return { hubId: null, sessionJwt: null }
      }
    }

    const useRootProxyIfAvailable = (): boolean => {
      const { hubId, sessionJwt } = readSession()
      if (!hubId || !sessionJwt) return false
      // We now use signed JWTs for root-proxy auth; legacy opaque `sess_*` cannot be validated statelessly.
      if (!sessionJwt.includes('.')) {
        this.invalidateWebSession()
        return false
      }
      if (!this.isJwtValid(sessionJwt)) {
        this.invalidateWebSession()
        return false
      }
      this.adaos.setBase(`https://api.inimatic.com/hubs/${hubId}`)
      this.adaos.setAuthBearer(sessionJwt)
      return true
    }

    const probeHttpStatus = async (
      baseUrl: string,
      timeoutMs = 800,
      headers?: Record<string, string>,
      // Root-proxy under `/hubs/<id>` does not expose `/healthz` on that prefix,
      // so probe `/api/ping` first to avoid noisy 404s in console/network logs.
      paths: string[] = ['/api/ping', '/healthz']
    ): Promise<number> => {
      const abs = baseUrl.replace(/\/$/, '')
      for (const p of paths) {
        try {
          const path = p.startsWith('/') ? p : `/${p}`
          const url = `${abs}${path}`
          const ctrl = new AbortController()
          const timer = setTimeout(() => ctrl.abort(), timeoutMs)
          try {
            const resp = await fetch(url, { method: 'GET', signal: ctrl.signal, headers })
            const st = resp.status || 0
            // 404 means "reachable but endpoint missing" – keep trying other probes.
            if (st === 404) continue
            return st
          } finally {
            clearTimeout(timer)
          }
        } catch {
          // keep trying other paths
        }
      }
      return 0
    }

    // Prefer a local hub on the same device if it is reachable (owner-device scenario),
    // even when the app is loaded from a public origin and a root-proxy session exists.
    const tryLocalHub = async (): Promise<boolean> => {
      const isLoopbackHost = (host: string): boolean => {
        const h = String(host || '').toLowerCase()
        return h === 'localhost' || h === '127.0.0.1' || h === '::1'
      }
      const isLoopbackUrl = (url: string): boolean => {
        try {
          return isLoopbackHost(new URL(url).hostname)
        } catch {
          return false
        }
      }
      const allowLoopback = (() => {
        try {
          const host = String(window.location.hostname || '')
          if (isLoopbackHost(host)) return true
        } catch {}
        try {
          const url = new URL(window.location.href)
          if (url.searchParams.get('try_local_hub') === '1') return true
        } catch {}
        try {
          return (localStorage.getItem('adaos_try_local_hub') || '').trim() === '1'
        } catch {
          return false
        }
      })()

      const candidates: string[] = []
      try {
        const persisted = (localStorage.getItem('adaos_hub_base') || '').trim()
        if (persisted && !isLoopbackUrl(persisted)) candidates.push(persisted)
      } catch {}
      if (allowLoopback) candidates.push('http://127.0.0.1:8777', 'http://localhost:8777')
      if (!candidates.length) return false
      for (const base of candidates) {
        const st = await probeHttpStatus(base, 650)
        if (st >= 200 && st < 300) {
          this.adaos.setBase(base)
          // Do not send Bearer JWT to a local hub; prefer X-AdaOS-Token (if provided) or no auth.
          const token = (globalThis as any)?.__ADAOS_TOKEN__ ?? null
          this.adaos.setAuthAdaosToken(token)
          try {
            localStorage.setItem('adaos_hub_base', base)
          } catch {}
          return true
        }
      }
      return false
    }

    // Avoid noisy loopback probes on SmartTV/mobile where 127.0.0.1 is never the hub.
    await tryLocalHub()

    // Prefer direct hub base, but if it is down (e.g. 127.0.0.1:8777 not responding),
    // automatically fall back to the root proxy route over NATS.
    const directBase = this.adaos.getBaseUrl().replace(/\/$/, '')
    const directStatus = await probeHttpStatus(directBase, 650)
    if (!(directStatus >= 200 && directStatus < 300)) {
      const switched = useRootProxyIfAvailable()
      if (!switched) {
        throw new Error('hub_unreachable_no_session')
      }

      // Validate session against root-proxy before attempting WS.
      const token = this.adaos.getToken()
      const rootBase = this.adaos.getBaseUrl().replace(/\/$/, '')
      const reachability = await probeHttpStatus(rootBase, 1200)
      if (reachability === 0) {
        throw new Error('hub_unreachable')
      }
      const rootStatus = await probeHttpStatus(
        rootBase,
        1600,
        token ? { Authorization: `Bearer ${token}` } : undefined,
        ['/api/node/status']
      )
      if (rootStatus === 401 || rootStatus === 403) {
        this.invalidateWebSession()
        throw new Error('session_invalid')
      }
      if (rootStatus === 0) {
        throw new Error('hub_unreachable')
      }
      if (rootStatus >= 500) {
        throw new Error('hub_offline')
      }
    }

    const baseHttp = this.adaos.getBaseUrl().replace(/\/$/, '')
    const baseWs = baseHttp.replace(/^http/, 'ws')

    // Ensure shared events websocket is connected and register device
    const fromUrl = this.getWebspaceFromUrl()
    const preferred = fromUrl || this.getPreferredWebspaceId()
    let webspaceId = String(preferred || 'default')
    try {
      await this.adaos.connect()
      const ack = await this.adaos.sendEventsCommand(
        'device.register',
        { device_id: this.deviceId, webspace_id: preferred },
        2500
      )
      webspaceId = String(ack?.data?.webspace_id || webspaceId)
    } catch (err) {
      // Best-effort: Yjs room can still be joined without device.register;
      // the hub will lazily create/seed the webspace on first yws join.
      try {
        if ((globalThis as any).__ADAOS_DEBUG__ === true) {
          // eslint-disable-next-line no-console
          console.warn('[YDocService] device.register failed; continuing', err)
        }
      } catch {}
    }
    this.currentWebspaceId = webspaceId
    this.setPreferredWebspaceId(webspaceId)

    // Initialise per-webspace IndexedDB persistence *after* webspace is known,
    // so that local snapshots do not leak state (such as ui/application/desktop)
    // across different webspaces.
    try {
      this.db = new IndexeddbPersistence(`adaos-mobile-${webspaceId}`, this.doc)
      // On some mobile browsers / private modes IndexedDB can hang indefinitely.
      // Do not block app startup on persistence.
      await Promise.race([
        this.db.whenSynced,
        new Promise<void>((resolve) => setTimeout(resolve, 1200)),
      ])
    } catch {
      // offline persistence is best-effort
    }

    // 2) Connect Yjs via y-websocket to /yws/<webspace_id>
    // WebsocketProvider builds URL as `${serverUrl}/${room}`.
    const serverUrl = `${baseWs}/yws`
    const room = webspaceId || 'default'
    this.provider = new WebsocketProvider(serverUrl, room, this.doc, {
      params: { dev: this.deviceId, ...(this.adaos.getToken() ? { token: String(this.adaos.getToken()) } : {}) },
    })

    await Promise.race([
      new Promise<void>((resolve) => {
      if (!this.provider) { resolve(); return }
      if (this.provider.synced) { resolve(); return }
      const handler = (synced: boolean) => {
        if (synced) {
          this.provider?.off('sync', handler as any)
          resolve()
        }
      }
      this.provider.on('sync', handler as any)
      }),
      new Promise<void>((_resolve, reject) =>
        setTimeout(() => reject(new Error('yjs_sync_timeout')), 9000),
      ),
    ])

  }

  getWebspaceId(): string {
    return this.currentWebspaceId
  }

  private getWebspaceFromUrl(): string | undefined {
    try {
      const url = new URL(window.location.href)
      return (
        url.searchParams.get('webspace_id') ||
        url.searchParams.get('webspace') ||
        undefined
      )
    } catch {
      return undefined
    }
  }

  private getPreferredWebspaceId(): string {
    try {
      return localStorage.getItem(this.webspaceKey) || 'default'
    } catch {
      return 'default'
    }
  }

  private setPreferredWebspaceId(id: string): void {
    try {
      localStorage.setItem(this.webspaceKey, id)
    } catch {}
  }

  async switchWebspace(webspaceId: string): Promise<void> {
    const target = (webspaceId || '').trim()
    if (!target || target === this.currentWebspaceId) return
    await this.adaos.sendEventsCommand('desktop.webspace.use', { webspace_id: target })
    this.setPreferredWebspaceId(target)
    try {
      const url = new URL(window.location.href)
      url.searchParams.set('webspace_id', target)
      window.location.href = url.toString()
    } catch {
      window.location.reload()
    }
  }

  getPath(path: string): any {
    const segs = path.split('/').filter(Boolean)
    let cur: any = this.doc.getMap(segs.shift()!)
    for (const s of segs) {
      if (cur instanceof Y.Map) cur = cur.get(s)
      else if (cur && typeof cur === 'object') cur = cur[s]
      else return undefined
    }
    return cur
  }

  toJSON(val: any): any {
    try {
      const anyVal: any = val
      if (anyVal && typeof anyVal.toJSON === 'function') return anyVal.toJSON()
    } catch {}
    return val
  }

  async clearStorage(): Promise<void> {
    try {
      const anyDb: any = this.db as any
      if (typeof anyDb.clearData === 'function') {
        await anyDb.clearData()
        return
      }
    } catch {}
    // Fallback: best-effort delete by DB name used in IndexeddbPersistence
    await new Promise<void>((resolve) => {
      try {
        const req = indexedDB.deleteDatabase('adaos-mobile')
        req.onsuccess = () => resolve()
        req.onerror = () => resolve()
        req.onblocked = () => resolve()
      } catch {
        resolve()
      }
    })
  }

  dumpSnapshot(): void {
    try {
      const ui = this.toJSON(this.getPath('ui'))
      const data = this.toJSON(this.getPath('data'))
      const registry = this.toJSON(this.getPath('registry'))
      // eslint-disable-next-line no-console
      console.log('[YDoc Snapshot]', { ui, data, registry })
    } catch {
      // ignore dump errors
    }
  }
}
