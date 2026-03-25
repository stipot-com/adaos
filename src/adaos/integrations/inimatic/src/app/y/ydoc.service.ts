import { Injectable } from '@angular/core'
import * as Y from 'yjs'
import { IndexeddbPersistence } from 'y-indexeddb'
import { WebsocketProvider } from 'y-websocket'
import { AdaosClient } from '../core/adaos/adaos-client.service'
import { DataChannelProvider } from './datachannel-provider'
import { isDebugEnabled } from '../debug-log'
import { LoginService } from '../features/login/login.service'
import { BehaviorSubject, firstValueFrom } from 'rxjs'
import { HubMemberChannelsService } from '../core/adaos/hub-member-channels.service'
import { HubMemberSyncRecoveryReason } from '../core/adaos/hub-member-channels.service'

export type YDocSyncConnectionState =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'disconnected'

@Injectable({ providedIn: 'root' })
export class YDocService {
  public readonly doc = new Y.Doc()
  private db?: IndexeddbPersistence
  private provider?: WebsocketProvider | DataChannelProvider
  readonly syncConnectionState$ = new BehaviorSubject<YDocSyncConnectionState>('idle')
  private initialized = false
  private initPromise?: Promise<void>
  private softReauthPromise?: Promise<void>
  private lastSoftReauthAttemptAt = 0
  private readonly deviceId: string
  private currentWebspaceId = 'default'
  private currentSyncPath: 'webrtc_data:yjs' | 'yws' | null = null
  private readonly webspaceKey = 'adaos_webspace_id'
  private readonly hubIdKey = 'adaos_hub_id'
  private readonly sessionJwtKey = 'adaos_web_session_jwt'
  private readonly yjsPersistKey = 'adaos_yjs_persist'

  constructor(
    private adaos: AdaosClient,
    private login: LoginService,
    private channels: HubMemberChannelsService,
  ) {
    this.deviceId = this.ensureDeviceId()
  }

  private isPersistenceEnabled(): boolean {
    try {
      const url = new URL(window.location.href)
      const q = url.searchParams.get('yjs_persist')
      if (q === '1' || q === 'true') return true
      if (q === '0' || q === 'false') return false
    } catch {}

    try {
      return (localStorage.getItem(this.yjsPersistKey) || '').trim() === '1'
    } catch {
      return false
    }
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

  private async trySoftReauthAndRecreateProvider(): Promise<void> {
    const now = Date.now()
    // Avoid hammering WebAuthn prompts on flapping networks.
    if (now - this.lastSoftReauthAttemptAt < 60_000) return
    this.lastSoftReauthAttemptAt = now
    try {
      // Do not trigger WebAuthn prompts in background tabs.
      if (typeof document !== 'undefined' && (document as any).hidden) return
    } catch {}

    try {
      if (isDebugEnabled()) {
        // eslint-disable-next-line no-console
        console.info('[YDocService] attempting soft re-auth via WebAuthn')
      }
      const res = await firstValueFrom(this.login.login())
      const jwt = String(res?.sessionJwt || '').trim()
      if (!jwt) throw new Error('soft_reauth_failed: no jwt')

      // LoginService persists jwt (and often hub id) to localStorage; ensure AdaosClient sees it.
      this.adaos.setAuthBearer(jwt)
      try {
        const hubId = this.pickHubIdFromJwt(jwt)
        if (hubId) localStorage.setItem(this.hubIdKey, hubId)
      } catch {}

      // Recreate WS provider with the fresh token (keep the Y.Doc intact).
      this.destroyCurrentProvider()

      const baseHttp = (this.adaos.getBaseUrl() || '').trim()
      const baseWs = baseHttp.replace(/^http/, 'ws')
      const serverUrl = `${baseWs}/yws`
      const room = this.currentWebspaceId || 'default'
      const syncPath = this.createSyncProvider(serverUrl, room)
      if (isDebugEnabled()) {
        // eslint-disable-next-line no-console
        console.info(
          `[YDocService] soft re-auth complete; sync provider recreated via ${syncPath.path}`,
        )
      }
    } catch (e) {
      if (isDebugEnabled()) {
        // eslint-disable-next-line no-console
        console.warn('[YDocService] soft re-auth failed', e)
      }
      // Fall back: keep retrying with existing provider, user can re-login manually.
    }
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

  private createSyncProvider(
    serverUrl: string,
    room: string,
    options?: {
      recoveryReason?: HubMemberSyncRecoveryReason | null
    },
  ): {
    provider: WebsocketProvider | DataChannelProvider
    path: 'webrtc_data:yjs' | 'yws'
  } {
    const syncPath = this.channels.createSyncProvider(
      this.doc,
      serverUrl,
      room,
      {
        dev: this.deviceId,
        ...(this.adaos.getToken() ? { token: String(this.adaos.getToken()) } : {}),
      },
      options ?? {},
    )
    this.currentSyncPath = syncPath.path as 'webrtc_data:yjs' | 'yws'
    this.provider = syncPath.provider
    this.syncConnectionState$.next('connecting')
    this.attachProviderConnectionSignals(this.provider)
    return {
      provider: syncPath.provider,
      path: syncPath.path as 'webrtc_data:yjs' | 'yws',
    }
  }

  private async waitForFirstSync(timeoutMs: number): Promise<boolean> {
    const provider = this.provider
    return Promise.race([
      new Promise<boolean>((resolve) => {
        if (!provider) {
          resolve(true)
          return
        }
        if ((provider as any).synced) {
          resolve(true)
          return
        }
        const handler = (synced: boolean) => {
          if (!synced) return
          try {
            ;(provider as any).off?.('sync', handler as any)
          } catch {}
          resolve(true)
        }
        ;(provider as any).on?.('sync', handler as any)
      }),
      new Promise<boolean>((resolve) => setTimeout(() => resolve(false), timeoutMs)),
    ])
  }

  private hasSeededDocContent(): boolean {
    try {
      const webspaces = this.toJSON(this.getPath('data/webspaces'))
      if (Array.isArray(webspaces?.items) && webspaces.items.length > 0) {
        return true
      }
    } catch {}
    try {
      const ui = this.toJSON(this.getPath('ui'))
      if (ui && typeof ui === 'object' && Object.keys(ui).length > 0) {
        return true
      }
    } catch {}
    try {
      const data = this.toJSON(this.getPath('data'))
      if (data && typeof data === 'object' && Object.keys(data).length > 0) {
        return true
      }
    } catch {}
    return false
  }

  private async attemptSemanticSyncRecovery(
    reason: HubMemberSyncRecoveryReason,
    {
      serverUrl,
      room,
      remoteProxy,
    }: {
      serverUrl: string
      room: string
      remoteProxy: boolean
    },
  ): Promise<boolean> {
    if (
      !this.channels.shouldRecoverSyncProvider({
        path: this.currentSyncPath,
        remoteProxy,
        hasSeededContent: this.hasSeededDocContent(),
      })
    ) {
      this.channels.recordSyncRecoverySkipped(reason)
      return false
    }
    this.destroyCurrentProvider()
    await new Promise<void>((resolve) => setTimeout(resolve, 250))
    try {
      const syncPath = this.createSyncProvider(serverUrl, room, {
        recoveryReason: reason,
      })
      if (isDebugEnabled()) {
        // eslint-disable-next-line no-console
        console.warn(
          `[YDocService] retrying sync provider via ${syncPath.path} reason=${reason}`,
        )
      }
      const ok = await this.waitForFirstSync(6000)
      if (!ok) {
        this.channels.recordSyncRecoveryFailed(reason)
      }
      return ok
    } catch (err) {
      this.channels.recordSyncRecoveryFailed(reason)
      if (isDebugEnabled()) {
        // eslint-disable-next-line no-console
        console.warn('[YDocService] sync provider recovery failed', err)
      }
      return false
    }
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
    this.destroyCurrentProvider()

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
      paths: string[] = ['/api/ping', '/healthz'],
      queryParams?: Record<string, string>
    ): Promise<number> => {
      const abs = baseUrl.replace(/\/$/, '')
      for (const p of paths) {
        try {
          const path = p.startsWith('/') ? p : `/${p}`
          const url = `${abs}${path}`
          const ctrl = new AbortController()
          const timer = setTimeout(() => ctrl.abort(), timeoutMs)
          try {
            const finalUrl = (() => {
              try {
                if (!queryParams || !Object.keys(queryParams).length) return url
                const u = new URL(url)
                for (const [k, v] of Object.entries(queryParams)) {
                  if (typeof v === 'string' && v) u.searchParams.set(k, v)
                }
                return u.toString()
              } catch {
                return url
              }
            })()
            const resp = await fetch(finalUrl, { method: 'GET', signal: ctrl.signal, headers })
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
      let warnedMixedContent = false
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
      const allowReservedLocalHub = (() => {
        try {
          const url = new URL(window.location.href)
          const q = (url.searchParams.get('try_local_hub') || '').trim().toLowerCase()
          if (q === '0' || q === 'false') return false
          if (q === '1' || q === 'true') return true
        } catch {}
        try {
          const v = (localStorage.getItem('adaos_try_local_hub') || '').trim()
          if (v === '0') return false
          if (v === '1') return true
        } catch {}
        return true
      })()
      const allowLoopback = (() => {
        // Arbitrary loopback ports are only tried on loopback origins
        // or when explicitly enabled.
        // - URL: ?try_local_hub=0
        // - localStorage: adaos_try_local_hub=0
        try {
          const url = new URL(window.location.href)
          const q = (url.searchParams.get('try_local_hub') || '').trim().toLowerCase()
          if (q === '0' || q === 'false') return false
          if (q === '1' || q === 'true') return true
        } catch {}
        try {
          const v = (localStorage.getItem('adaos_try_local_hub') || '').trim()
          if (v === '0') return false
          if (v === '1') return true
        } catch {}
        try {
          const host = String(window.location.hostname || '')
          return isLoopbackHost(host)
        } catch {}
        return false
      })()

      const candidates: string[] = []
      if (allowReservedLocalHub)
        candidates.push(
          'http://127.0.0.1:8777',
          'http://localhost:8777',
          'http://127.0.0.1:8778',
          'http://localhost:8778'
        )
      if (allowLoopback)
        candidates.push(
          'http://127.0.0.1:8778',
          'http://localhost:8778'
        )
      try {
        const persisted = (localStorage.getItem('adaos_hub_base') || '').trim()
        if (persisted) {
          const isReservedLocal =
            (() => {
              try {
                const parsed = new URL(persisted)
                return isLoopbackHost(parsed.hostname) && (parsed.port || '80') === '8777'
              } catch {
                return false
              }
            })()
          // Persisted hub base is an explicit browser choice. Keep honoring it
          // even on a public origin so custom local ports (for example 8778)
          // continue to work after reload.
          if (!isLoopbackUrl(persisted) || allowLoopback || allowReservedLocalHub || isReservedLocal) {
            candidates.push(persisted)
          }
        }
      } catch {}
      if (!candidates.length) return false
      for (const base of candidates) {
        const loopback = isLoopbackUrl(base)
        if (!warnedMixedContent && loopback) {
          try {
            if (window.location.protocol === 'https:' && String(base || '').startsWith('http://')) {
              warnedMixedContent = true
              if (isDebugEnabled()) {
                // eslint-disable-next-line no-console
                console.warn(
                  '[YDocService] Local hub probe may be blocked by mixed-content rules (HTTPS page -> HTTP localhost). ' +
                    'Consider opening the web client over http://localhost or enabling HTTPS/WSS on the hub.'
                )
              }
            }
          } catch {}
        }
        const authQuery = (() => {
          // If a persisted base is actually the root-proxy `/hubs/<id>` route,
          // it requires session JWT even for `/api/ping` probes.
          try {
            const abs = String(base || '').replace(/\/$/, '')
            if (!abs.includes('/hubs/')) return undefined
            const { sessionJwt } = readSession()
            if (!sessionJwt) return undefined
            if (!sessionJwt.includes('.')) return undefined
            if (!this.isJwtValid(sessionJwt)) return undefined
            return { session_jwt: sessionJwt }
          } catch {
            return undefined
          }
        })()
        const token = (() => {
          const globalToken = (globalThis as any)?.__ADAOS_TOKEN__ ?? null
          if (globalToken) return globalToken
          try {
            const v = (localStorage.getItem('adaos_hub_token') || '').trim()
            return v ? v : null
          } catch {
            return null
          }
        })()
        // Use a simple unauthenticated probe to avoid CORS preflights and mixed-content noise.
        const st = await probeHttpStatus(base, 650, undefined, undefined, authQuery)
        if (st >= 200 && st < 300) {
          this.adaos.setBase(base)
          // Do not send Bearer JWT to a local hub; prefer X-AdaOS-Token (if provided) or no auth.
          // Transparent auth for local runs (MVP): fall back to the default dev token.
          this.adaos.setAuthAdaosToken(token || 'dev-local-token')
          try {
            localStorage.setItem('adaos_hub_base', base)
          } catch {}
          return true
        }
      }
      return false
    }

    const hasBoundSession = useRootProxyIfAvailable()

    // Only probe transparent local hubs during the pre-login / pre-binding stage.
    // Once the browser is already bound to a subnet, go directly to the root-proxy
    // hub route and avoid localhost probing delays on every reconnect/reload.
    if (!hasBoundSession) {
      // `127.0.0.1:8777` remains the primary transparent local-hub entrypoint for
      // app.inimatic.com. Also probe `8778`, because local dev nodes commonly bind
      // there and the browser can still reach that hub directly on the same device.
      await tryLocalHub()
    }

    // Prefer direct hub base, but if it is down (e.g. 127.0.0.1:8777 not responding),
    // automatically fall back to the root proxy route over NATS.
    const directBase = this.adaos.getBaseUrl().replace(/\/$/, '')
    const isDefinitelyNotAHubBase = (() => {
      try {
        const abs = String(directBase || '').replace(/\/+$/, '')
        // Root base (no /hubs/<id>) can respond 200 to /api/ping, but it is not a hub API base.
        if (abs.includes('/hubs/')) return false
        const u = new URL(abs)
        const host = (u.hostname || '').toLowerCase()
        if (host === 'localhost' || host === '127.0.0.1' || host === '::1') return false
        // Anything remote without /hubs/<id> is treated as "not a hub base".
        return true
      } catch {
        return false
      }
    })()

    const directIsLoopback = (() => {
      try {
        const u = new URL(directBase)
        const host = (u.hostname || '').toLowerCase()
        return host === 'localhost' || host === '127.0.0.1' || host === '::1'
      } catch {
        return false
      }
    })()

    const directStatus = isDefinitelyNotAHubBase
      ? 0
      : await probeHttpStatus(directBase, 650, directIsLoopback ? undefined : this.adaos.getAuthHeaders())
    if (!(directStatus >= 200 && directStatus < 300)) {
      const switched = useRootProxyIfAvailable()
      if (!switched) {
        throw new Error('hub_unreachable_no_session')
      }

      // Validate session against root-proxy before attempting WS.
      const token = this.adaos.getToken()
      const authQuery = token ? { session_jwt: String(token) } : undefined
      const rootBase = this.adaos.getBaseUrl().replace(/\/$/, '')
      const reachability = await probeHttpStatus(
        rootBase,
        1200,
        undefined,
        ['/api/ping', '/healthz'],
        authQuery
      )
      if (reachability === 0) {
        throw new Error('hub_unreachable')
      }
      const rootStatus = await probeHttpStatus(
        rootBase,
        1600,
        undefined,
        ['/api/node/status'],
        authQuery
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

    // IndexedDB persistence can cause "stale UI" issues during active schema/scenario
    // development because the browser may replay an old local snapshot back into Yjs
    // and overwrite a freshly seeded server doc (e.g. after `desktop.webspace.reload`).
    // For now we keep persistence opt-in.
    if (this.isPersistenceEnabled()) {
      try {
        // Initialise per-webspace IndexedDB persistence *after* webspace is known,
        // so that local snapshots do not leak state across different webspaces.
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
    } else if (isDebugEnabled()) {
      // eslint-disable-next-line no-console
      console.info('[YDocService] IndexedDB persistence disabled (set ?yjs_persist=1 to enable)')
    }

    // 2) Attempt WebRTC upgrade when using root-proxy (hub behind NAT)
    const isRemoteProxy = baseHttp.includes('/hubs/')
    let webRtcActive = false
    if (isRemoteProxy) {
      try {
        const prepared = await this.adaos.prepareMemberTransport()
        webRtcActive = prepared.direct
      } catch {
        // WebRTC negotiation failed — continue with WS
      }
    } else if (isDebugEnabled()) {
      // eslint-disable-next-line no-console
      console.info('[YDocService] direct member transport skipped outside routed hub proxy')
    }

    // 3) Connect Yjs through the semantic sync channel rather than branching
    // transport adapters directly in application code.
    const serverUrl = `${baseWs}/yws`
    const room = webspaceId || 'default'
    const syncPath = this.createSyncProvider(serverUrl, room)
    if (isDebugEnabled()) {
      // eslint-disable-next-line no-console
      console.info(
        `[YDocService] sync channel selected path=${syncPath.path} webRtcActive=${webRtcActive}`,
      )
    }

    // Do not fail app startup on a slow/unstable WS path. The provider will keep reconnecting in the background.
    // We still wait a bit for "first sync" to provide fast feedback, but treat timeout as degraded mode.
    const firstSyncOrTimeout = await this.waitForFirstSync(9000)
    if (!firstSyncOrTimeout) {
      if (isDebugEnabled()) {
        // eslint-disable-next-line no-console
        console.warn('[YDocService] yws sync timeout; continuing and waiting for reconnect')
      }
      if (isRemoteProxy && syncPath.path === 'yws' && !this.hasSeededDocContent()) {
        await this.attemptSemanticSyncRecovery('first_sync_timeout', {
          serverUrl,
          room,
          remoteProxy: isRemoteProxy,
        })
      }
    }

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
    // Fallback: best-effort delete by DB name used in IndexeddbPersistence.
    // Historically we used `adaos-mobile` (global). Now it's per-webspace.
    const webspaceId = (this.currentWebspaceId || '').trim()
    const names = [
      webspaceId ? `adaos-mobile-${webspaceId}` : null,
      'adaos-mobile',
    ].filter(Boolean) as string[]

    for (const name of names) {
      await new Promise<void>((resolve) => {
        try {
          const req = indexedDB.deleteDatabase(name)
          req.onsuccess = () => resolve()
          req.onerror = () => resolve()
          req.onblocked = () => resolve()
        } catch {
          resolve()
        }
      })
    }
  }

  private attachProviderConnectionSignals(provider: WebsocketProvider | DataChannelProvider | undefined): void {
    try {
      const p: any = provider as any
      if (!p || typeof p.on !== 'function') return
      p.on('sync', (synced: any) => {
        try {
          const connected = Boolean(synced)
          this.syncConnectionState$.next(connected ? 'connected' : 'disconnected')
          this.channels.reportSyncPathState(
            this.currentSyncPath,
            connected ? 'connected' : 'disconnected',
          )
        } catch {}
      })
      p.on('status', (ev: any) => {
        try {
          const st = String(ev?.status || '').trim().toLowerCase()
          if (st === 'connected') {
            this.syncConnectionState$.next('connected')
            this.channels.reportSyncPathState(this.currentSyncPath, 'connected')
            return
          }
          if (st !== 'disconnected') return
          this.syncConnectionState$.next('disconnected')
          this.channels.reportSyncPathState(this.currentSyncPath, 'disconnected')
          if (this.currentSyncPath === 'yws') {
            const baseHttp = this.adaos.getBaseUrl().replace(/\/$/, '')
            const serverUrl = `${baseHttp.replace(/^http/, 'ws')}/yws`
            const room = this.currentWebspaceId || 'default'
            void this.attemptSemanticSyncRecovery('provider_disconnected', {
              serverUrl,
              room,
              remoteProxy: baseHttp.includes('/hubs/'),
            })
          }
          const jwt = (localStorage.getItem(this.sessionJwtKey) || '').trim()
          if (!jwt || !jwt.includes('.')) return
          if (!this.isJwtValid(jwt)) {
            // Token is expired: attempt a soft re-auth (WebAuthn) and recreate provider without reload.
            if (!this.softReauthPromise) {
              this.softReauthPromise = this.trySoftReauthAndRecreateProvider().finally(() => {
                this.softReauthPromise = undefined
              })
            }
          }
        } catch {}
      })
    } catch {}
  }

  private destroyCurrentProvider(): void {
    const lastPath = this.currentSyncPath
    try {
      this.provider?.destroy()
    } catch {}
    this.provider = undefined
    this.currentSyncPath = null
    this.syncConnectionState$.next('idle')
    this.channels.reportSyncPathState(lastPath, 'idle')
  }

  dumpSnapshot(): void {
    try {
      const ui = this.toJSON(this.getPath('ui'))
      const data = this.toJSON(this.getPath('data'))
      const registry = this.toJSON(this.getPath('registry'))
      if (isDebugEnabled()) {
        // eslint-disable-next-line no-console
        console.log('[YDoc Snapshot]', { ui, data, registry })
      }
    } catch {
      // ignore dump errors
    }
  }
}
