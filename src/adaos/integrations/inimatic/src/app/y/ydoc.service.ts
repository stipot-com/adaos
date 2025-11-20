import { Injectable } from '@angular/core'
import * as Y from 'yjs'
import { IndexeddbPersistence } from 'y-indexeddb'
import { WebsocketProvider } from 'y-websocket'
import { AdaosClient } from '../core/adaos/adaos-client.service'

@Injectable({ providedIn: 'root' })
export class YDocService {
  public readonly doc = new Y.Doc()
  private readonly db = new IndexeddbPersistence('adaos-mobile', this.doc)
  private provider?: WebsocketProvider
  private initialized = false
  private initPromise?: Promise<void>
  private readonly deviceId: string
  private currentWebspaceId = 'default'
  private readonly webspaceKey = 'adaos_webspace_id'

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
    return this.initPromise
  }

  private async doInitFromHub(): Promise<void> {
    // Keep local IndexedDB sync for offline/dev convenience
    try { await this.db.whenSynced } catch {}

    const baseHttp = this.adaos.getBaseUrl().replace(/\/$/, '')
    const baseWs = baseHttp.replace(/^http/, 'ws')

    // Ensure shared events websocket is connected and register device
    await this.adaos.connect()
    const preferred = this.getPreferredWebspaceId()
    const ack = await this.adaos.sendEventsCommand('device.register', { device_id: this.deviceId, webspace_id: preferred })
    const webspaceId = String(ack?.data?.webspace_id || preferred || 'default')
    this.currentWebspaceId = webspaceId
    this.setPreferredWebspaceId(webspaceId)

    // 2) Connect Yjs via y-websocket to /yws/<webspace_id>
    // WebsocketProvider builds URL as `${serverUrl}/${room}`.
    const serverUrl = `${baseWs}/yws`
    const room = webspaceId || 'default'
    this.provider = new WebsocketProvider(serverUrl, room, this.doc, {
      params: { dev: this.deviceId },
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

    this.initialized = true
  }

  getWebspaceId(): string {
    return this.currentWebspaceId
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
    window.location.reload()
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
}
