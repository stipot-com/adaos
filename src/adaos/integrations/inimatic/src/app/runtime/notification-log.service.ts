import { Injectable } from '@angular/core'
import { ToastController } from '@ionic/angular/standalone'
import { BehaviorSubject } from 'rxjs'
import { YDocService } from '../y/ydoc.service'

export type NotificationHistoryEntry = {
  id: string
  ts: string
  level: string
  message: string
  source?: string
  code?: string
}

type NotificationShowOptions = {
  duration?: number
  color?: string
  position?: 'top' | 'bottom' | 'middle'
  source?: string
  code?: string
  log?: boolean
}

const STORAGE_KEY = 'adaos.notification_history.v1'
const MAX_ITEMS = 200

@Injectable({ providedIn: 'root' })
export class NotificationLogService {
  private readonly entriesSubject = new BehaviorSubject<NotificationHistoryEntry[]>(
    this.readStoredEntries(),
  )
  readonly entries$ = this.entriesSubject.asObservable()
  private readonly ydocSeenKeys = new Set<string>()
  private localCounter = 0

  constructor(
    private toastCtrl: ToastController,
    private ydoc: YDocService,
  ) {}

  getSnapshot(): NotificationHistoryEntry[] {
    this.syncFromYDoc()
    return [...this.entriesSubject.value].sort((left, right) =>
      String(right.ts || '').localeCompare(String(left.ts || '')),
    )
  }

  clear(): void {
    this.entriesSubject.next([])
    this.persist([])
  }

  record(entry: Partial<NotificationHistoryEntry>): NotificationHistoryEntry {
    this.syncFromYDoc()
    const next: NotificationHistoryEntry = {
      id: this.nextLocalId(),
      ts: String(entry.ts || new Date().toISOString()),
      level: String(entry.level || 'info').trim() || 'info',
      message: String(entry.message || '').trim(),
      source: String(entry.source || '').trim() || undefined,
      code: String(entry.code || '').trim() || undefined,
    }
    if (!next.message) {
      return next
    }
    const items = [...this.entriesSubject.value, next].slice(-MAX_ITEMS)
    this.entriesSubject.next(items)
    this.persist(items)
    return next
  }

  async show(message: string, opts: NotificationShowOptions = {}): Promise<void> {
    const text = String(message || '').trim()
    if (!text) return
    if (opts.log !== false) {
      this.record({
        level: this.levelFromColor(opts.color),
        message: text,
        source: opts.source,
        code: opts.code,
      })
    }
    const toast = await this.toastCtrl.create({
      message: text,
      duration: opts.duration ?? 2200,
      position: opts.position ?? 'bottom',
      color: opts.color as any,
    })
    await toast.present()
  }

  syncFromYDoc(): void {
    try {
      const raw = this.ydoc.toJSON(this.ydoc.getPath('data/desktop/toasts'))
      if (!Array.isArray(raw) || !raw.length) return
      let items = [...this.entriesSubject.value]
      let changed = false
      for (const item of raw) {
        if (!item || typeof item !== 'object') continue
        const entry: NotificationHistoryEntry = {
          id: this.ydocKeyFor(item),
          ts: String((item as any)?.['ts'] || new Date().toISOString()),
          level: String((item as any)?.['level'] || 'info').trim() || 'info',
          message: String((item as any)?.['message'] || '').trim(),
          source: String((item as any)?.['source'] || 'runtime').trim() || 'runtime',
          code: String((item as any)?.['code'] || '').trim() || undefined,
        }
        if (!entry.message || this.ydocSeenKeys.has(entry.id)) continue
        this.ydocSeenKeys.add(entry.id)
        items = [...items, entry].slice(-MAX_ITEMS)
        changed = true
      }
      if (changed) {
        this.entriesSubject.next(items)
        this.persist(items)
      }
    } catch {
      // best-effort only
    }
  }

  private readStoredEntries(): NotificationHistoryEntry[] {
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      if (!raw) return []
      const parsed = JSON.parse(raw)
      if (!Array.isArray(parsed)) return []
      return parsed
        .filter((item) => item && typeof item === 'object')
        .map((item) => ({
          id: String(item.id || this.nextLocalId()),
          ts: String(item.ts || new Date().toISOString()),
          level: String(item.level || 'info').trim() || 'info',
          message: String(item.message || '').trim(),
          source: String(item.source || '').trim() || undefined,
          code: String(item.code || '').trim() || undefined,
        }))
        .filter((item) => !!item.message)
        .slice(-MAX_ITEMS)
    } catch {
      return []
    }
  }

  private persist(items: NotificationHistoryEntry[]): void {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(items.slice(-MAX_ITEMS)))
    } catch {
      // ignore storage errors
    }
  }

  private nextLocalId(): string {
    this.localCounter += 1
    return `local:${Date.now()}:${this.localCounter}`
  }

  private ydocKeyFor(item: any): string {
    return [
      String(item?.['ts'] || ''),
      String(item?.['level'] || ''),
      String(item?.['source'] || ''),
      String(item?.['code'] || ''),
      String(item?.['message'] || ''),
    ].join('|')
  }

  private levelFromColor(color?: string): string {
    const normalized = String(color || '').trim().toLowerCase()
    if (!normalized) return 'info'
    if (normalized === 'danger') return 'error'
    if (normalized === 'success') return 'success'
    if (normalized === 'warning') return 'warning'
    return normalized
  }
}
