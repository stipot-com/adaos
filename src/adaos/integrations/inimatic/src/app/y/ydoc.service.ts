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

    // 1) device.register over /ws
    const baseWs = baseHttp.replace(/^http/, 'ws')

    const eventsUrl = `${baseWs}/ws`
    const ws = new WebSocket(eventsUrl)
    await new Promise<void>((resolve, reject) => {
      const onOpen = () => { cleanup(); resolve() }
      const onError = (ev: Event) => { cleanup(); reject(ev) }
      const cleanup = () => {
        ws.removeEventListener('open', onOpen)
        ws.removeEventListener('error', onError)
      }
      ws.addEventListener('open', onOpen)
      ws.addEventListener('error', onError)
    })

    const cmdId = `device.register.${Date.now()}`
    ws.send(JSON.stringify({
      ch: 'events',
      t: 'cmd',
      id: cmdId,
      kind: 'device.register',
      payload: { device_id: this.deviceId },
    }))

    const workspaceId = await new Promise<string>((resolve, reject) => {
      const timeout = setTimeout(() => {
        ws.close()
        reject(new Error('device.register timeout'))
      }, 5000)
      const onMessage = (ev: MessageEvent) => {
        try {
          const msg = JSON.parse(ev.data)
          if (msg?.ch === 'events' && msg?.t === 'ack' && msg?.id === cmdId && msg?.ok && msg?.data?.workspace_id) {
            clearTimeout(timeout)
            cleanup()
            resolve(String(msg.data.workspace_id))
          }
        } catch { /* ignore */ }
      }
      const onError = () => {
        clearTimeout(timeout)
        cleanup()
        reject(new Error('events websocket error'))
      }
      const cleanup = () => {
        ws.removeEventListener('message', onMessage)
        ws.removeEventListener('error', onError)
      }
      ws.addEventListener('message', onMessage)
      ws.addEventListener('error', onError)
    })

    // 2) Connect Yjs via y-websocket to /yws
    // WebsocketProvider builds URL as `${serverUrl}/${room}`. To hit `/yws`
    // with query params, we encode them into the "room" segment.
    const serverUrl = baseWs
    const room = `yws?ws=${encodeURIComponent(workspaceId)}&dev=${encodeURIComponent(this.deviceId)}`
    this.provider = new WebsocketProvider(serverUrl, room, this.doc)

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
