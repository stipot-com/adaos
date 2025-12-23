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
        return {
          hubId: localStorage.getItem(this.hubIdKey),
          sessionJwt: localStorage.getItem(this.sessionJwtKey),
        }
      } catch {
        return { hubId: null, sessionJwt: null }
      }
    }

    const useRootProxyIfAvailable = (): boolean => {
      const { hubId, sessionJwt } = readSession()
      if (!hubId || !sessionJwt) return false
      this.adaos.setBase(`https://api.inimatic.com/hubs/${hubId}`)
      this.adaos.setAuthBearer(sessionJwt)
      return true
    }

    const probeHttp = async (baseUrl: string, timeoutMs = 800): Promise<boolean> => {
      try {
        const abs = baseUrl.replace(/\/$/, '')
        const url = `${abs}/api/ping`
        const ctrl = new AbortController()
        const timer = setTimeout(() => ctrl.abort(), timeoutMs)
        try {
          const resp = await fetch(url, { method: 'GET', signal: ctrl.signal })
          return resp.ok
        } finally {
          clearTimeout(timer)
        }
      } catch {
        return false
      }
    }

    // Prefer direct hub base, but if it is down (e.g. 127.0.0.1:8777 not responding),
    // automatically fall back to the root proxy route over NATS.
    const directBase = this.adaos.getBaseUrl().replace(/\/$/, '')
    const directOk = await probeHttp(directBase, 650)
    if (!directOk) {
      const switched = useRootProxyIfAvailable()
      if (!switched) {
        throw new Error('hub_unreachable_no_session')
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
      // eslint-disable-next-line no-console
      console.warn('[YDocService] device.register failed; continuing', err)
    }
    this.currentWebspaceId = webspaceId
    this.setPreferredWebspaceId(webspaceId)

    // Initialise per-webspace IndexedDB persistence *after* webspace is known,
    // so that local snapshots do not leak state (such as ui/application/desktop)
    // across different webspaces.
    try {
      this.db = new IndexeddbPersistence(`adaos-mobile-${webspaceId}`, this.doc)
      await this.db.whenSynced
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

    await new Promise<void>((resolve) => {
      if (!this.provider) { resolve(); return }
      if (this.provider.synced) { resolve(); return }
      const handler = (synced: boolean) => {
        if (synced) {
          this.provider?.off('sync', handler as any)
          resolve()
        }
      }
      this.provider.on('sync', handler as any)
    })

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
