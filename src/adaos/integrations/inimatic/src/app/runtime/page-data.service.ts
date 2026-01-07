import { Injectable } from '@angular/core'
import { HttpClient } from '@angular/common/http'
import { Observable, of } from 'rxjs'
import { map } from 'rxjs/operators'
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

let YDOC_DEBUG_EMITS = 0

@Injectable({ providedIn: 'root' })
export class PageDataService {
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
    const body = this.resolveParams(cfg.params)
    // AdaosClient.callSkill currently returns an Observable<T>
    return this.adaos.callSkill<T>(skill, method, body)
  }

  private fromApi<T>(cfg: ApiDataSource): Observable<T | undefined> {
    const url = cfg.url
    if (!url) return of(undefined)
    const body = this.resolveParams(cfg.body)
    const params = this.resolveParams(cfg.params)
    const method = cfg.method || 'GET'
    if (method === 'GET') {
      return this.http
        .get<T>(url, { params: params as any })
        .pipe(map((v) => v as T))
    }
    if (method === 'DELETE') {
      return this.http
        .delete<T>(url, { params: params as any })
        .pipe(map((v) => v as T))
    }
    if (method === 'POST') {
      return this.http
        .post<T>(url, body, { params: params as any })
        .pipe(map((v) => v as T))
    }
    if (method === 'PUT') {
      return this.http
        .put<T>(url, body, { params: params as any })
        .pipe(map((v) => v as T))
    }
    if (method === 'PATCH') {
      return this.http
        .patch<T>(url, body, { params: params as any })
        .pipe(map((v) => v as T))
    }
    return of(undefined)
  }

  private resolveParams(input: any): any {
    if (!input || typeof input !== 'object') return input
    const state = this.state.getSnapshot()
    const out: any = {}
    for (const [k, v] of Object.entries(input)) {
      if (typeof v === 'string' && v.startsWith('$state.')) {
        const key = v.slice('$state.'.length)
        out[k] = state[key]
      } else {
        out[k] = v
      }
    }
    return out
  }

  private fromYDoc<T>(cfg: YDocDataSource): Observable<T | undefined> {
    return new Observable<T | undefined>((subscriber) => {
      const emit = () => {
        const value = this.computeYDocValue(cfg) as T
        YDOC_DEBUG_EMITS++
        if (YDOC_DEBUG_EMITS <= 20) {
          try {
            const kind = cfg.transform || cfg.path || 'unknown'
            const size =
              Array.isArray(value) ? `len=${value.length}` : value && typeof value === 'object' ? 'object' : typeof value
            // eslint-disable-next-line no-console
            console.log('[PageDataService] fromYDoc emit', kind, size)
          } catch {
            // ignore logging errors
          }
        }
        subscriber.next(value)
      }
      const unsubscribers = this.observeYDocPaths(cfg, emit)
      emit()
      return () => {
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
    try {
      // eslint-disable-next-line no-console
      console.log(
        '[PageDataService] resolveDesktopIcons',
        'catalogApps=',
        catalogApps.length,
        'installedApps=',
        installedApps.length,
        'resolved=',
        items.length
      )
    } catch {}
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
