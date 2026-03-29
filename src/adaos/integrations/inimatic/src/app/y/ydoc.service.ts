import { Injectable } from '@angular/core'
import * as Y from 'yjs'
import { IndexeddbPersistence } from 'y-indexeddb'
import { WebsocketProvider } from 'y-websocket'
import { AdaosClient, rootHubBaseUrl } from '../core/adaos/adaos-client.service'
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

export type YDocSyncResyncReason =
  | HubMemberSyncRecoveryReason
  | 'manual'

export type YDocSyncRuntimeSnapshot = {
  updatedAt: number
  webspaceId: string
  initialized: boolean
  initInFlight: boolean
  persistenceEnabled: boolean
  connectionState: YDocSyncConnectionState
  currentPath: 'webrtc_data:yjs' | 'yws' | null
  resyncCount: number
  lastResyncAt: number | null
  lastResyncReason: YDocSyncResyncReason | null
  lastResyncOk: boolean | null
  lastResyncClearLocalCache: boolean
  lastProviderCreatedAt: number | null
  lastFirstSyncTimeoutAt: number | null
  lastProviderDisconnectedAt: number | null
  lastSoftReauthAt: number | null
  awareness: {
    ephemeral: true
    localStatePresent: boolean
    totalStateCount: number
    remoteStateCount: number
    updateCount: number
    lastUpdateAt: number | null
  }
}

export type YDocMaterializationSnapshot = {
  updatedAt: number
  webspaceId: string
  currentScenario: string | null
  hasUiApplication: boolean
  hasDesktopConfig: boolean
  hasDesktopPageSchema: boolean
  hasAppsCatalogModal: boolean
  hasWidgetsCatalogModal: boolean
  hasCatalogApps: boolean
  hasCatalogWidgets: boolean
  catalogAppsCount: number
  catalogWidgetsCount: number
  topbarCount: number
  pageWidgetCount: number
  ready: boolean
}

function getGlobalScope(): any {
  if (typeof globalThis !== 'undefined') return globalThis as any
  if (typeof window !== 'undefined') return window as any
  if (typeof self !== 'undefined') return self as any
  return {} as any
}

@Injectable({ providedIn: 'root' })
export class YDocService {
  public readonly doc = new Y.Doc()
  private db?: IndexeddbPersistence
  private provider?: WebsocketProvider | DataChannelProvider
  readonly syncConnectionState$ = new BehaviorSubject<YDocSyncConnectionState>('idle')
  readonly syncRuntime$ = new BehaviorSubject<YDocSyncRuntimeSnapshot>(
    this.buildSyncRuntimeSnapshot(),
  )
  private initialized = false
  private initPromise?: Promise<void>
  private softReauthPromise?: Promise<void>
  private lastSoftReauthAttemptAt = 0
  private readonly deviceId: string
  private currentWebspaceId = 'default'
  private currentSyncPath: 'webrtc_data:yjs' | 'yws' | null = null
  private readonly webspaceKey = 'adaos_webspace_id'
  private readonly webspaceReturnMapKey = 'adaos_webspace_return_map'
  private readonly hubIdKey = 'adaos_hub_id'
  private readonly sessionJwtKey = 'adaos_web_session_jwt'
  private readonly yjsPersistKey = 'adaos_yjs_persist'
  private resyncCount = 0
  private lastResyncAt: number | null = null
  private lastResyncReason: YDocSyncResyncReason | null = null
  private lastResyncOk: boolean | null = null
  private lastResyncClearLocalCache = false
  private lastProviderCreatedAt: number | null = null
  private lastFirstSyncTimeoutAt: number | null = null
  private lastProviderDisconnectedAt: number | null = null
  private lastSoftReauthAt: number | null = null
  private awarenessUpdateCount = 0
  private lastAwarenessUpdateAt: number | null = null
  private awarenessLocalStatePresent = false
  private awarenessTotalStateCount = 0
  private awarenessRemoteStateCount = 0
  private awarenessCleanup?: () => void
  private providerFirstSyncTimer: ReturnType<typeof setTimeout> | null = null
  private providerHasSynced = false
  private resyncInFlight?: Promise<boolean>
  private resyncInFlightKey: string | null = null

  constructor(
    private adaos: AdaosClient,
    private login: LoginService,
    private channels: HubMemberChannelsService,
  ) {
    this.deviceId = this.ensureDeviceId()
  }

  private buildSyncRuntimeSnapshot(): YDocSyncRuntimeSnapshot {
    return {
      updatedAt: Date.now(),
      webspaceId: this.currentWebspaceId || 'default',
      initialized: this.initialized,
      initInFlight: !!this.initPromise,
      persistenceEnabled: this.isPersistenceEnabled(),
      connectionState: this.syncConnectionState$.value,
      currentPath: this.currentSyncPath,
      resyncCount: this.resyncCount,
      lastResyncAt: this.lastResyncAt,
      lastResyncReason: this.lastResyncReason,
      lastResyncOk: this.lastResyncOk,
      lastResyncClearLocalCache: this.lastResyncClearLocalCache,
      lastProviderCreatedAt: this.lastProviderCreatedAt,
      lastFirstSyncTimeoutAt: this.lastFirstSyncTimeoutAt,
      lastProviderDisconnectedAt: this.lastProviderDisconnectedAt,
      lastSoftReauthAt: this.lastSoftReauthAt,
      awareness: {
        ephemeral: true,
        localStatePresent: this.awarenessLocalStatePresent,
        totalStateCount: this.awarenessTotalStateCount,
        remoteStateCount: this.awarenessRemoteStateCount,
        updateCount: this.awarenessUpdateCount,
        lastUpdateAt: this.lastAwarenessUpdateAt,
      },
    }
  }

  private publishSyncRuntime(): void {
    this.syncRuntime$.next(this.buildSyncRuntimeSnapshot())
  }

  private setSyncConnectionState(state: YDocSyncConnectionState): void {
    this.syncConnectionState$.next(state)
    this.publishSyncRuntime()
  }

  private clearProviderFirstSyncWatchdog(): void {
    try {
      if (this.providerFirstSyncTimer) {
        clearTimeout(this.providerFirstSyncTimer)
      }
    } catch {}
    this.providerFirstSyncTimer = null
  }

  private armProviderFirstSyncWatchdog(
    provider: WebsocketProvider | DataChannelProvider | undefined,
    timeoutMs = 9000,
  ): void {
    this.clearProviderFirstSyncWatchdog()
    if (!provider || this.provider !== provider) return
    try {
      if ((provider as any)?.synced) return
    } catch {}
    this.providerFirstSyncTimer = setTimeout(() => {
      if (!provider || this.provider !== provider) return
      try {
        if ((provider as any)?.synced) return
      } catch {}
      this.lastFirstSyncTimeoutAt = Date.now()
      this.publishSyncRuntime()
      const baseHttp = this.adaos.getBaseUrl().replace(/\/$/, '')
      const serverUrl = `${baseHttp.replace(/^http/, 'ws')}/yws`
      const room = this.currentWebspaceId || 'default'
      const remoteProxy = baseHttp.includes('/hubs/')
      if (this.currentSyncPath === 'yws') {
        this.setSyncConnectionState('connecting')
        this.channels.reportSyncPathState(this.currentSyncPath, 'connecting')
        void this.attemptSemanticSyncRecovery('first_sync_timeout', {
          serverUrl,
          room,
          remoteProxy,
        })
      }
    }, Math.max(1000, timeoutMs))
  }

  private refreshAwarenessState(awareness: any | undefined | null): void {
    try {
      const states = awareness?.getStates?.()
      if (!states || typeof states.forEach !== 'function') {
        this.awarenessLocalStatePresent = false
        this.awarenessTotalStateCount = 0
        this.awarenessRemoteStateCount = 0
        return
      }
      let total = 0
      let remote = 0
      let localPresent = false
      states.forEach((value: any, clientId: any) => {
        total += 1
        if (clientId === this.doc.clientID) {
          localPresent = value != null
        } else {
          remote += 1
        }
      })
      this.awarenessLocalStatePresent = localPresent
      this.awarenessTotalStateCount = total
      this.awarenessRemoteStateCount = remote
    } catch {
      this.awarenessLocalStatePresent = false
      this.awarenessTotalStateCount = 0
      this.awarenessRemoteStateCount = 0
    }
  }

  private attachAwarenessSignals(provider: WebsocketProvider | DataChannelProvider | undefined): void {
    try {
      this.awarenessCleanup?.()
    } catch {}
    this.awarenessCleanup = undefined
    const awareness = (provider as any)?.awareness
    if (!awareness || typeof awareness.on !== 'function') {
      this.refreshAwarenessState(null)
      this.publishSyncRuntime()
      return
    }
    const onUpdate = () => {
      this.lastAwarenessUpdateAt = Date.now()
      this.awarenessUpdateCount += 1
      this.refreshAwarenessState(awareness)
      this.publishSyncRuntime()
    }
    try {
      awareness.on('update', onUpdate)
      this.refreshAwarenessState(awareness)
      this.publishSyncRuntime()
      this.awarenessCleanup = () => {
        try {
          awareness.off?.('update', onUpdate)
        } catch {}
      }
    } catch {
      this.refreshAwarenessState(null)
      this.publishSyncRuntime()
    }
  }

  getSyncRuntimeSnapshot(): YDocSyncRuntimeSnapshot {
    return this.buildSyncRuntimeSnapshot()
  }

  getMaterializationSnapshot(): YDocMaterializationSnapshot {
    const webspaceId = this.currentWebspaceId || 'default'
    const currentScenarioRaw = this.toJSON(this.getPath('ui/current_scenario'))
    const currentScenario =
      typeof currentScenarioRaw === 'string' && currentScenarioRaw.trim()
        ? currentScenarioRaw.trim()
        : null

    const application = this.toJSON(this.getPath('ui/application')) || {}
    const desktop = application?.desktop || {}
    const modals = application?.modals || {}
    const catalog = this.toJSON(this.getPath('data/catalog')) || {}
    const apps = Array.isArray(catalog?.apps) ? catalog.apps : null
    const widgets = Array.isArray(catalog?.widgets) ? catalog.widgets : null
    const pageSchema = desktop?.pageSchema
    const topbar = Array.isArray(desktop?.topbar) ? desktop.topbar : []
    const pageWidgets = Array.isArray(pageSchema?.widgets) ? pageSchema.widgets : []

    const hasUiApplication = !!application && typeof application === 'object' && !Array.isArray(application)
    const hasDesktopConfig = !!desktop && typeof desktop === 'object' && !Array.isArray(desktop)
    const hasDesktopPageSchema =
      !!pageSchema && typeof pageSchema === 'object' && !Array.isArray(pageSchema)
    const hasAppsCatalogModal = !!modals && typeof modals === 'object' && 'apps_catalog' in modals
    const hasWidgetsCatalogModal =
      !!modals && typeof modals === 'object' && 'widgets_catalog' in modals
    const hasCatalogApps = Array.isArray(apps)
    const hasCatalogWidgets = Array.isArray(widgets)

    return {
      updatedAt: Date.now(),
      webspaceId,
      currentScenario,
      hasUiApplication,
      hasDesktopConfig,
      hasDesktopPageSchema,
      hasAppsCatalogModal,
      hasWidgetsCatalogModal,
      hasCatalogApps,
      hasCatalogWidgets,
      catalogAppsCount: hasCatalogApps ? apps.length : 0,
      catalogWidgetsCount: hasCatalogWidgets ? widgets.length : 0,
      topbarCount: topbar.length,
      pageWidgetCount: pageWidgets.length,
      ready:
        hasUiApplication &&
        hasDesktopConfig &&
        hasDesktopPageSchema &&
        hasAppsCatalogModal &&
        hasWidgetsCatalogModal &&
        hasCatalogApps &&
        hasCatalogWidgets,
    }
  }

  hasMaterializedDesktopContent(): boolean {
    return this.getMaterializationSnapshot().ready
  }

  async waitForMaterializedDesktopContent(timeoutMs = 6000): Promise<boolean> {
    if (this.hasMaterializedDesktopContent()) return true
    const startedAt = Date.now()
    while (Date.now() - startedAt < Math.max(250, timeoutMs)) {
      await new Promise((resolve) => setTimeout(resolve, 150))
      if (this.hasMaterializedDesktopContent()) return true
    }
    return this.hasMaterializedDesktopContent()
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
      const g = getGlobalScope()
      const cryptoApi = g.crypto as Crypto | undefined
      const raw =
        (cryptoApi && typeof (cryptoApi as any).randomUUID === 'function'
          ? (cryptoApi as any).randomUUID()
          : null) ||
        Math.random().toString(36).slice(2)
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
    this.lastSoftReauthAt = now
    this.publishSyncRuntime()
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

      const baseHttp = (this.adaos.getBaseUrl() || '').trim()
      const room = this.currentWebspaceId || 'default'
      const ok = await this.resyncCurrentWebspace({
        reason: 'soft_reauth',
        remoteProxy: baseHttp.includes('/hubs/'),
        room,
      })
      if (isDebugEnabled()) {
        // eslint-disable-next-line no-console
        console.info(
          `[YDocService] soft re-auth complete; sync provider recreated ok=${ok}`,
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
    this.providerHasSynced = Boolean((this.provider as any)?.synced)
    this.lastProviderCreatedAt = Date.now()
    this.setSyncConnectionState(this.providerHasSynced ? 'connected' : 'connecting')
    this.attachAwarenessSignals(this.provider)
    this.attachProviderConnectionSignals(this.provider)
    if (!this.providerHasSynced) {
      this.armProviderFirstSyncWatchdog(this.provider)
    }
    this.publishSyncRuntime()
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

  private hasRecoverableDocContent(): boolean {
    return this.getMaterializationSnapshot().ready
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
        reason,
        path: this.currentSyncPath,
        remoteProxy,
        hasSeededContent: this.hasRecoverableDocContent(),
      })
    ) {
      this.channels.recordSyncRecoverySkipped(reason)
      return false
    }
    return this.resyncCurrentWebspace({
      reason,
      serverUrl,
      room,
      remoteProxy,
      waitForFirstSyncTimeoutMs: 6000,
    })
  }

  async resyncCurrentWebspace({
    reason = 'manual',
    clearLocalCache = false,
    serverUrl,
    room,
    remoteProxy,
    waitForFirstSyncTimeoutMs = 6000,
  }: {
    reason?: YDocSyncResyncReason
    clearLocalCache?: boolean
    serverUrl?: string
    room?: string
    remoteProxy?: boolean
    waitForFirstSyncTimeoutMs?: number
  } = {}): Promise<boolean> {
    const baseHttp = (this.adaos.getBaseUrl() || '').trim().replace(/\/$/, '')
    const resolvedServerUrl = serverUrl || `${baseHttp.replace(/^http/, 'ws')}/yws`
    const resolvedRoom = room || this.currentWebspaceId || 'default'
    const resolvedRemoteProxy =
      typeof remoteProxy === 'boolean' ? remoteProxy : baseHttp.includes('/hubs/')
    const requestKey = JSON.stringify({
      reason,
      clearLocalCache: !!clearLocalCache,
      room: resolvedRoom,
      serverUrl: resolvedServerUrl,
      remoteProxy: resolvedRemoteProxy,
    })

    if (this.resyncInFlight) {
      if (this.resyncInFlightKey === requestKey || this.syncConnectionState$.value === 'connecting') {
        return this.resyncInFlight
      }
    }

    const run = (async (): Promise<boolean> => {
      this.lastResyncAt = Date.now()
      this.lastResyncReason = reason
      this.lastResyncClearLocalCache = !!clearLocalCache
      this.lastResyncOk = null
      this.publishSyncRuntime()

      if (clearLocalCache && this.isPersistenceEnabled()) {
        await this.clearStorage()
      }

      this.destroyCurrentProvider()
      await new Promise<void>((resolve) => setTimeout(resolve, 250))
      try {
        const syncPath = this.createSyncProvider(resolvedServerUrl, resolvedRoom, {
          recoveryReason: reason,
        })
        if (isDebugEnabled()) {
          // eslint-disable-next-line no-console
          console.warn(
            `[YDocService] resyncing provider via ${syncPath.path} reason=${reason} remoteProxy=${resolvedRemoteProxy}`,
          )
        }
        const ok = await this.waitForFirstSync(waitForFirstSyncTimeoutMs)
        this.lastResyncOk = ok
        if (!ok) {
          this.channels.recordSyncRecoveryFailed(reason)
        }
        this.publishSyncRuntime()
        return ok
      } catch (err) {
        this.lastResyncOk = false
        this.channels.recordSyncRecoveryFailed(reason)
        this.publishSyncRuntime()
        if (isDebugEnabled()) {
          // eslint-disable-next-line no-console
          console.warn('[YDocService] sync provider resync failed', err)
        }
        return false
      } finally {
        this.resyncCount += 1
        this.publishSyncRuntime()
      }
    })()

    this.resyncInFlight = run
    this.resyncInFlightKey = requestKey
    try {
      return await run
    } finally {
      if (this.resyncInFlight === run) {
        this.resyncInFlight = undefined
        this.resyncInFlightKey = null
      }
    }
  }

  async initFromHub(): Promise<void> {
    if (this.initialized) return
    if (this.initPromise) return this.initPromise
    this.publishSyncRuntime()
    this.initPromise = this.doInitFromHub()
      .then(() => {
        this.initialized = true
        this.publishSyncRuntime()
      })
      .finally(() => {
        // Allow retries after failures (e.g. hub offline until user logs in to use root-proxy).
        this.initPromise = undefined
        this.publishSyncRuntime()
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
      this.adaos.setBase(rootHubBaseUrl(hubId))
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
          const globalToken = getGlobalScope().__ADAOS_TOKEN__ ?? null
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
        if (getGlobalScope().__ADAOS_DEBUG__ === true) {
          // eslint-disable-next-line no-console
          console.warn('[YDocService] device.register failed; continuing', err)
        }
      } catch {}
    }
    this.currentWebspaceId = webspaceId
    this.setPreferredWebspaceId(webspaceId)
    this.publishSyncRuntime()

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
      this.lastFirstSyncTimeoutAt = Date.now()
      this.publishSyncRuntime()
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

  private readReturnWebspaceMap(): Record<string, string> {
    try {
      const raw = localStorage.getItem(this.webspaceReturnMapKey)
      const parsed = raw ? JSON.parse(raw) : {}
      if (!parsed || typeof parsed !== 'object') return {}
      const out: Record<string, string> = {}
      for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
        const target = String(key || '').trim()
        const source = String(value || '').trim()
        if (!target || !source || target === source) continue
        out[target] = source
      }
      return out
    } catch {
      return {}
    }
  }

  private writeReturnWebspaceMap(map: Record<string, string>): void {
    try {
      localStorage.setItem(this.webspaceReturnMapKey, JSON.stringify(map))
    } catch {}
  }

  rememberReturnWebspace(targetWebspaceId: string, returnToWebspaceId: string): void {
    const target = String(targetWebspaceId || '').trim()
    const source = String(returnToWebspaceId || '').trim()
    if (!target || !source || target === source) return
    const map = this.readReturnWebspaceMap()
    map[target] = source
    this.writeReturnWebspaceMap(map)
  }

  getReturnWebspaceId(webspaceId?: string): string | undefined {
    const key = String(webspaceId || this.currentWebspaceId || '').trim()
    if (!key) return undefined
    const map = this.readReturnWebspaceMap()
    const value = String(map[key] || '').trim()
    return value || undefined
  }

  clearReturnWebspaceId(webspaceId?: string): void {
    const key = String(webspaceId || this.currentWebspaceId || '').trim()
    if (!key) return
    const map = this.readReturnWebspaceMap()
    if (!(key in map)) return
    delete map[key]
    this.writeReturnWebspaceMap(map)
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

  openWebspaceInNewWindow(webspaceId: string): boolean {
    const target = (webspaceId || '').trim()
    if (!target) return false
    try {
      const url = new URL(window.location.href)
      url.searchParams.set('webspace_id', target)
      const opened = window.open(url.toString(), '_blank', 'noopener')
      return !!opened
    } catch {
      return false
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
          if (this.provider !== provider) return
          const connected = Boolean(synced)
          this.providerHasSynced = connected
          if (connected) {
            this.clearProviderFirstSyncWatchdog()
          } else {
            this.armProviderFirstSyncWatchdog(provider)
          }
          this.setSyncConnectionState(connected ? 'connected' : 'disconnected')
          this.channels.reportSyncPathState(
            this.currentSyncPath,
            connected ? 'connected' : 'disconnected',
          )
        } catch {}
      })
      p.on('status', (ev: any) => {
        try {
          if (this.provider !== provider) return
          const st = String(ev?.status || '').trim().toLowerCase()
          if (st === 'connected') {
            const ready = this.providerHasSynced || Boolean(p?.synced)
            if (ready) {
              this.setSyncConnectionState('connected')
              this.channels.reportSyncPathState(this.currentSyncPath, 'connected')
              this.clearProviderFirstSyncWatchdog()
            } else {
              this.setSyncConnectionState('connecting')
              this.channels.reportSyncPathState(this.currentSyncPath, 'connecting')
              this.armProviderFirstSyncWatchdog(provider)
            }
            return
          }
          if (st !== 'disconnected') return
          this.clearProviderFirstSyncWatchdog()
          this.lastProviderDisconnectedAt = Date.now()
          this.setSyncConnectionState('disconnected')
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
    try {
      this.awarenessCleanup?.()
    } catch {}
    this.awarenessCleanup = undefined
    this.provider = undefined
    this.currentSyncPath = null
    this.providerHasSynced = false
    this.clearProviderFirstSyncWatchdog()
    this.refreshAwarenessState(null)
    this.setSyncConnectionState('idle')
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
