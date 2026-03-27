import { Injectable } from '@angular/core'
import { HttpClient } from '@angular/common/http'
import { Observable, of } from 'rxjs'
import { catchError, map, shareReplay } from 'rxjs/operators'
import {
  ApiDataSource,
  DataSourceConfig,
  SkillDataSource,
  StaticDataSource,
  YDocDataSource,
  WidgetConfig,
  WidgetType,
} from './page-schema.model'
import { PageStateService } from './page-state.service'
import { AdaosClient } from '../core/adaos/adaos-client.service'
import { YDocService } from '../y/ydoc.service'
import { observeDeep } from '../y/y-helpers'


@Injectable({ providedIn: 'root' })
export class PageDataService {
  private readonly infrastateSnapshotTtlMs = 1000
  private readonly infrastateSnapshotCache = new Map<string, { at: number; stream: Observable<any | undefined> }>()

  constructor(
    private http: HttpClient,
    private state: PageStateService,
    private adaos: AdaosClient,
    private ydoc: YDocService
  ) {}

  load<T = any>(cfg: DataSourceConfig | undefined): Observable<T | undefined> {
    if (!cfg) return of(undefined)
    if (cfg.kind === 'static') return this.fromStatic<T>(cfg)
    if (cfg.kind === 'skill') return this.fromSkill<T>(cfg)
    if (cfg.kind === 'api') return this.fromApi<T>(cfg)
    if (cfg.kind === 'y') return this.fromYDoc<T>(cfg)
    return of(undefined)
  }

  private fromStatic<T>(cfg: StaticDataSource): Observable<T> {
    return of(cfg.value as T)
  }

  private fromSkill<T>(cfg: SkillDataSource): Observable<T | undefined> {
    const [skill, method] = cfg.name.split('.', 2)
    if (!skill || !method) return of(undefined)
    const bodyResolution = this.resolveParams(cfg.params)
    if (bodyResolution.missingStateRefs.length) return of(undefined)
    const body = bodyResolution.value
    // AdaosClient.callSkill currently returns an Observable<T>
    return this.adaos
      .callSkill<T>(skill, method, body)
      .pipe(catchError((err) => this.recoverLoadFailure<T>('skill', `${skill}.${method}`, err)))
  }

  private fromApi<T>(cfg: ApiDataSource): Observable<T | undefined> {
    const url = cfg.url
    if (!url) return of(undefined)
    const bodyResolution = this.resolveParams(cfg.body)
    const paramsResolution = this.resolveParams(cfg.params)
    if (bodyResolution.missingStateRefs.length || paramsResolution.missingStateRefs.length) {
      return of(undefined)
    }
    const body = bodyResolution.value
    const params = paramsResolution.value
    const method = cfg.method || 'GET'
    const absUrl = this.absUrl(url)
    const headers: any = this.adaos.getAuthHeaders ? this.adaos.getAuthHeaders() : {}
    if (method === 'GET') {
      return this.http
        .get<T>(absUrl, { params: params as any, headers })
        .pipe(map((v) => v as T), catchError((err) => this.recoverLoadFailure<T>('api', absUrl, err)))
    }
    if (method === 'DELETE') {
      return this.http
        .delete<T>(absUrl, { params: params as any, headers })
        .pipe(map((v) => v as T), catchError((err) => this.recoverLoadFailure<T>('api', absUrl, err)))
    }
    if (method === 'POST') {
      return this.http
        .post<T>(absUrl, body, { params: params as any, headers })
        .pipe(map((v) => v as T), catchError((err) => this.recoverLoadFailure<T>('api', absUrl, err)))
    }
    if (method === 'PUT') {
      return this.http
        .put<T>(absUrl, body, { params: params as any, headers })
        .pipe(map((v) => v as T), catchError((err) => this.recoverLoadFailure<T>('api', absUrl, err)))
    }
    if (method === 'PATCH') {
      return this.http
        .patch<T>(absUrl, body, { params: params as any, headers })
        .pipe(map((v) => v as T), catchError((err) => this.recoverLoadFailure<T>('api', absUrl, err)))
    }
    return of(undefined)
  }

  private recoverLoadFailure<T>(
    kind: 'skill' | 'api',
    target: string,
    err: unknown
  ): Observable<T | undefined> {
    try {
      console.warn(`PageDataService ${kind} load failed`, { target, err })
    } catch {}
    return of(undefined)
  }

  private absUrl(url: string): string {
    const raw = String(url || '').trim()
    if (!raw) return raw
    if (/^https?:\/\//i.test(raw)) return raw
    const base = String(this.adaos.getBaseUrl ? this.adaos.getBaseUrl() : '').replace(/\/$/, '')
    const rel = raw.startsWith('/') ? raw : `/${raw}`
    return `${base}${rel}`
  }

  private isInfrastatePath(path?: string): boolean {
    return !!path && (path === 'data/infrastate' || path.startsWith('data/infrastate/'))
  }

  private loadInfrastateSnapshot(webspaceId: string): Observable<any | undefined> {
    const now = Date.now()
    const cached = this.infrastateSnapshotCache.get(webspaceId)
    if (cached && now - cached.at <= this.infrastateSnapshotTtlMs) {
      return cached.stream
    }
    const rel = `/api/node/infrastate/snapshot?webspace_id=${encodeURIComponent(webspaceId)}`
    const headers: any = this.adaos.getAuthHeaders ? this.adaos.getAuthHeaders() : {}
    const stream = this.http
      .get<{ ok?: boolean; snapshot?: any }>(this.absUrl(rel), { headers })
      .pipe(
        map((res) => res?.snapshot),
        catchError((err) => this.recoverLoadFailure<any>('api', rel, err)),
        shareReplay({ bufferSize: 1, refCount: false }),
      )
    this.infrastateSnapshotCache.set(webspaceId, { at: now, stream })
    return stream
  }

  private loadInfrastateFallback(path?: string): Observable<any | undefined> {
    const webspaceId = this.adaos.getCurrentWebspaceId?.() || 'default'
    return this.loadInfrastateSnapshot(webspaceId).pipe(
      map((snapshot) => this.pickInfrastateSnapshotValue(snapshot, path)),
    )
  }

  private readInfrastateRootFromYDoc(): any {
    const root = this.ydoc.toJSON(this.ydoc.getPath('data')) || {}
    return root?.infrastate
  }

  private hasLiveInfrastateSnapshot(): boolean {
    const root = this.readInfrastateRootFromYDoc()
    if (!root || typeof root !== 'object' || Array.isArray(root)) return false
    const lastRefresh = Number((root as any)?.last_refresh_ts || 0)
    if (!Number.isFinite(lastRefresh) || lastRefresh <= 0) return false
    return true
  }

  private pickInfrastateSnapshotValue(snapshot: any, path?: string): any {
    if (!path || !this.isInfrastatePath(path)) return snapshot
    const segs = path.split('/').filter(Boolean)
    let cur: any =
      snapshot && typeof snapshot === 'object' && 'infrastate' in snapshot
        ? snapshot?.infrastate
        : snapshot
    const logicalSegs = segs[0] === 'data' ? segs.slice(1) : segs
    const projectionSegs =
      logicalSegs[0] === 'infrastate'
        ? logicalSegs.slice(1)
        : logicalSegs
    for (const s of projectionSegs) {
      if (cur == null) return undefined
      cur = cur?.[s]
    }
    return cur
  }

  private resolveParams(input: any): { value: any; missingStateRefs: string[] } {
    if (!input || typeof input !== 'object') {
      return { value: input, missingStateRefs: [] }
    }
    const state = this.state.getSnapshot()
    const out: any = {}
    const missingStateRefs: string[] = []
    for (const [k, v] of Object.entries(input)) {
      if (typeof v === 'string' && v.startsWith('$state.')) {
        const key = v.slice('$state.'.length)
        const resolved = state[key]
        if (resolved === undefined || resolved === null || resolved === '') {
          missingStateRefs.push(key)
          continue
        }
        out[k] = resolved
      } else {
        out[k] = v
      }
    }
    return { value: out, missingStateRefs }
  }

  private fromYDoc<T>(cfg: YDocDataSource): Observable<T | undefined> {
    return new Observable<T | undefined>((subscriber) => {
      let infrastateFallbackRequested = false
      let fallbackSubscription: { unsubscribe(): void } | null = null

      const emit = () => {
        const value = this.computeYDocValue(cfg) as T
        subscriber.next(value)
        if (
          this.isInfrastatePath(cfg.path) &&
          !infrastateFallbackRequested &&
          !this.hasLiveInfrastateSnapshot()
        ) {
          infrastateFallbackRequested = true
          fallbackSubscription = this.loadInfrastateFallback(cfg.path).subscribe((fallback) => {
            if (fallback !== undefined) {
              subscriber.next(fallback as T)
            }
          })
        }
      }
      const unsubscribers = this.observeYDocPaths(cfg, emit)
      emit()
      return () => {
        try {
          fallbackSubscription?.unsubscribe()
        } catch {}
        unsubscribers.forEach((fn) => {
          try {
            fn()
          } catch {}
        })
      }
    })
  }

  private observeYDocPaths(cfg: YDocDataSource, emit: () => void): Array<() => void> {
    // Special-case desktop icons/widgets: observe whole data tree like legacy member desktop.
    if (cfg.transform === 'desktop.icons' || cfg.transform === 'desktop.widgets') {
      const node = this.ydoc.getPath('data')
      if (!node) return [() => {}]
      const unsubscribe = observeDeep(node, emit)
      return [unsubscribe]
    }

    // Special-case weather snapshot: observe whole data tree, because
    // server-side code replaces data.weather/current map instances.
    if (cfg.path === 'data/weather/current') {
      const node = this.ydoc.getPath('data')
      if (!node) return [() => {}]
      const unsubscribe = observeDeep(node, emit)
      return [unsubscribe]
    }

    // Prompt IDE workflow and LLM artifacts: observe whole data tree,
    // because the server stores prompt state as plain JSON under data.prompt.*.
    if (cfg.path && cfg.path.startsWith('data/prompt/')) {
      const node = this.ydoc.getPath('data')
      if (!node) return [() => {}]
      const unsubscribe = observeDeep(node, emit)
      return [unsubscribe]
    }

    // Voice chat + TTS queues: server mutates nested plain JSON under data.voice_chat / data.tts.
    // Observe the whole data tree so updates are delivered reliably.
    if (cfg.path && (cfg.path === 'data/voice_chat' || cfg.path.startsWith('data/voice_chat/') || cfg.path === 'data/tts' || cfg.path.startsWith('data/tts/'))) {
      const node = this.ydoc.getPath('data')
      if (!node) return [() => {}]
      const unsubscribe = observeDeep(node, emit)
      return [unsubscribe]
    }

    // Teacher artifacts are stored as plain JSON under data.nlu_teacher (not Y.Maps),
    // so observe the whole data map and project the subpath from it.
    if (cfg.path && (cfg.path === 'data/nlu_teacher' || cfg.path.startsWith('data/nlu_teacher/'))) {
      const node = this.ydoc.getPath('data')
      if (!node) return [() => {}]
      const unsubscribe = observeDeep(node, emit)
      return [unsubscribe]
    }

    // Infrastate projections are written as a plain JSON subtree under
    // data.infrastate, not as nested Y.Maps. Observe the whole data map so
    // modal widgets keep updating when the snapshot arrives or refreshes.
    if (cfg.path && (cfg.path === 'data/infrastate' || cfg.path.startsWith('data/infrastate/'))) {
      const node = this.ydoc.getPath('data')
      if (!node) return [() => {}]
      const unsubscribe = observeDeep(node, emit)
      return [unsubscribe]
    }

    const paths = this.pathsForYDoc(cfg)
    if (!paths.length) return [() => {}]
    return paths.map((path) => {
      const node = path ? this.getYNode(path) : undefined
      if (node) return observeDeep(node, emit)
      return () => {}
    })
  }

  private pathsForYDoc(cfg: YDocDataSource): string[] {
    switch (cfg.transform) {
      case 'desktop.icons':
        return ['data']
      case 'desktop.widgets':
        return ['data']
      default:
        return cfg.path ? [cfg.path] : []
    }
  }

  private computeYDocValue(cfg: YDocDataSource): any {
    switch (cfg.transform) {
      case 'desktop.icons':
        return this.resolveDesktopIcons()
      case 'desktop.widgets':
        return this.resolveDesktopWidgets()
      default:
        if (cfg.path === 'data/weather/current') {
          // Weather snapshot is sometimes seeded as a plain dict under
          // data.weather, so YDoc.getPath('data/weather/current') may
          // not work reliably. Read the whole data map as JSON and
          // project weather.current from it.
          const root = this.ydoc.toJSON(this.ydoc.getPath('data')) || {}
          return root?.weather?.current
        }
        if (cfg.path && (cfg.path === 'data/voice_chat' || cfg.path.startsWith('data/voice_chat/') || cfg.path === 'data/tts' || cfg.path.startsWith('data/tts/'))) {
          const root = this.ydoc.toJSON(this.ydoc.getPath('data')) || {}
          const segs = cfg.path.split('/').filter(Boolean)
          // segs[0] is "data"
          let cur: any = root
          for (const s of segs.slice(1)) {
            if (cur == null) return undefined
            cur = cur?.[s]
          }
          return cur
        }
        if (cfg.path && (cfg.path === 'data/nlu_teacher' || cfg.path.startsWith('data/nlu_teacher/'))) {
          const root = this.ydoc.toJSON(this.ydoc.getPath('data')) || {}
          const segs = cfg.path.split('/').filter(Boolean)
          let cur: any = root
          for (const s of segs.slice(1)) {
            if (cur == null) return undefined
            cur = cur?.[s]
          }
          return cur
        }
        if (cfg.path && (cfg.path === 'data/infrastate' || cfg.path.startsWith('data/infrastate/'))) {
          const root = this.ydoc.toJSON(this.ydoc.getPath('data')) || {}
          const segs = cfg.path.split('/').filter(Boolean)
          let cur: any = root
          for (const s of segs.slice(1)) {
            if (cur == null) return undefined
            cur = cur?.[s]
          }
          return cur
        }
        if (cfg.path) {
          return this.ydoc.toJSON(this.ydoc.getPath(cfg.path))
        }
        return undefined
    }
  }

  private resolveDesktopIcons(): Array<{ id: string; title: string; icon: string; action?: any; dev?: boolean }> {
    const catalogApps: any[] = this.ydoc.toJSON(this.ydoc.getPath('data/catalog/apps')) || []
    const installedApps = this.readInstalled('apps')
    const app = this.ydoc.toJSON(this.ydoc.getPath('ui/application')) || {}
    const iconTemplate = app?.desktop?.iconTemplate?.icon || 'apps-outline'
    const byId: Record<string, any> = {}
    for (const it of catalogApps) {
      if (it?.id) byId[it.id] = it
    }
    const items = installedApps
      .map((id) => byId[id])
      .filter(Boolean)
      .map((it) => ({
        id: it.id,
        title: it.title || it.id,
        icon: it.icon || iconTemplate,
        action: it.launchModal ? { openModal: it.launchModal } : undefined,
        scenario_id: it.scenario_id,
        dev: !!it.dev,
      }))
    return items
  }

  private resolveDesktopWidgets(): WidgetConfig[] {
    const catalogWidgets: any[] = this.ydoc.toJSON(this.ydoc.getPath('data/catalog/widgets')) || []
    const installedWidgets = this.readInstalled('widgets')
    const app: any = this.ydoc.toJSON(this.ydoc.getPath('ui/application')) || {}
    const pinnedRaw = app?.desktop?.pinnedWidgets
    const pinnedWidgets: any[] = Array.isArray(pinnedRaw) ? pinnedRaw : []
    const byId: Record<string, any> = {}
    for (const it of catalogWidgets) {
      if (it?.id) byId[it.id] = it
    }

    const normalize = (raw: any): WidgetConfig | undefined => {
      if (!raw || typeof raw !== 'object') return undefined
      const id = raw.id != null ? String(raw.id) : ''
      if (!id) return undefined
      const base = byId[id]
      const merged: any =
        base && typeof base === 'object'
          ? { ...base, ...raw } // pinned overrides win over catalog
          : { ...raw }

      const type = String(merged.type || 'visual.metricTile') as WidgetType
      merged.id = id
      merged.type = type
      return merged as WidgetConfig
    }

    const pinned = pinnedWidgets
      .filter((it) => it && typeof it === 'object' && it.id)
      .map((it) => normalize(it))
      .filter(Boolean) as WidgetConfig[]

    const installed = installedWidgets
      .map((id) => normalize(byId[id]))
      .filter(Boolean) as WidgetConfig[]

    const seen = new Set<string>()
    const out: WidgetConfig[] = []
    for (const item of [...pinned, ...installed]) {
      if (!item?.id) continue
      if (seen.has(item.id)) continue
      seen.add(item.id)
      out.push(item)
    }
    return out
  }

  private getYNode(path: string): any {
    const segments = path.split('/').filter(Boolean)
    if (!segments.length) return undefined
    const [root, ...rest] = segments
    let current: any = this.ydoc.doc.getMap(root)
    for (const seg of rest) {
      if (!current || typeof current.get !== 'function') {
        return undefined
      }
      current = current.get(seg)
    }
    return current
  }

  private readInstalled(kind: 'apps' | 'widgets'): string[] {
    const path = `data/installed/${kind}`
    const raw = this.ydoc.toJSON(this.ydoc.getPath(path))
    const list = this.normalizeInstalledList(raw)
    return list
  }

  private normalizeInstalledList(raw: any): string[] {
    if (Array.isArray(raw)) {
      return raw.filter((v): v is string => typeof v === 'string')
    }
    if (raw && typeof raw === 'object') {
      if (Array.isArray(raw.items)) {
        return raw.items.filter((v: any): v is string => typeof v === 'string')
      }
      if (Array.isArray(raw.value)) {
        return raw.value.filter((v: any): v is string => typeof v === 'string')
      }
    }
    return []
  }
}
