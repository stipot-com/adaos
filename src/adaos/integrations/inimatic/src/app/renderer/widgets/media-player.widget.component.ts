import {
  Component,
  ElementRef,
  Input,
  OnChanges,
  OnDestroy,
  OnInit,
  SimpleChanges,
  ViewChild,
} from '@angular/core'
import { CommonModule } from '@angular/common'
import {
  HttpClient,
  HttpEvent,
  HttpEventType,
  HttpHeaders,
} from '@angular/common/http'
import { FormsModule } from '@angular/forms'
import { IonicModule } from '@ionic/angular'
import { firstValueFrom, Subscription } from 'rxjs'
import {
  HubMemberChannelSnapshot,
  HubMemberChannelsService,
} from '../../core/adaos/hub-member-channels.service'
import { AdaosClient } from '../../core/adaos/adaos-client.service'
import { PageModalService } from '../../runtime/page-modal.service'
import { WidgetConfig } from '../../runtime/page-schema.model'

type MediaLibraryItem = {
  name: string
  size_bytes: number
  mime_type: string
  modified_at: string
  content_path: string
}

type MediaCapabilities = {
  upload?: Record<string, any>
  playback?: Record<string, any>
  broadcast?: Record<string, any>
  notes?: string[]
  storage?: Record<string, any>
}

type MediaLibraryResponse = {
  ok: boolean
  items: MediaLibraryItem[]
  count?: number
  total_bytes?: number
  capabilities?: MediaCapabilities
  proxy_limits?: {
    root_routed_response_limit_bytes?: number
  }
}

@Component({
  selector: 'ada-media-player-widget',
  standalone: true,
  imports: [CommonModule, FormsModule, IonicModule],
  providers: [PageModalService],
  template: `
    <div class="media-widget" [class.compact]="compact">
      <div class="media-toolbar">
        <div class="media-toolbar__title">
          <h2 *ngIf="widget?.title">{{ widget.title }}</h2>
          <div class="media-toolbar__route">{{ routeLabel }}</div>
        </div>

        <div class="media-toolbar__actions">
          <ion-button size="small" fill="outline" (click)="refreshLibrary()" [disabled]="loading">
            Refresh
          </ion-button>
          <ion-button
            *ngIf="compact && modalId"
            size="small"
            fill="outline"
            (click)="openModal()"
          >
            Open
          </ion-button>
          <input
            #fileInput
            hidden
            type="file"
            accept="video/*,.mp4,.webm,.ogv,.ogg,.mov,.m4v,.mkv,.avi,.wmv"
            (change)="onFilePicked($event)"
          />
          <ion-button
            size="small"
            (click)="fileInput.click()"
            [disabled]="!uploadAllowed || uploading"
          >
            Upload
          </ion-button>
        </div>
      </div>

      <div class="media-status" *ngIf="showDiagnostics">
        <div class="media-status__grid">
          <div class="media-pill" [attr.data-state]="uploadAllowed ? 'ready' : 'blocked'">
            Upload: {{ uploadLabel }}
          </div>
          <div class="media-pill" [attr.data-state]="playbackLabelState">
            Playback: {{ playbackLabel }}
          </div>
          <div class="media-pill" data-state="blocked">
            Broadcast: {{ broadcastLabel }}
          </div>
          <div class="media-pill" [attr.data-state]="routeHealthState">
            Link: {{ routeHealthLabel }}
          </div>
        </div>
        <div class="media-note" *ngIf="channelSnapshot">
          Media policy: {{ channelSnapshot.mediaPolicy.mode }} ({{ channelSnapshot.mediaPolicy.reason }})
        </div>
        <div class="media-note" *ngIf="!uploadAllowed">
          Routed <code>/hubs/&lt;id&gt;/api/*</code> path is buffered JSON/base64 today, so upload is intentionally disabled here.
        </div>
        <div class="media-note" *ngIf="selectedTooLargeForRouted">
          This file is larger than the current routed proxy safe body limit ({{ formatBytes(proxyLimitBytes) }}), so inline video playback is disabled on the routed path.
        </div>
      </div>

      <ion-progress-bar *ngIf="uploading" [value]="uploadProgress / 100"></ion-progress-bar>
      <div class="media-note" *ngIf="uploading">
        Uploading {{ uploadName || 'file' }}: {{ uploadProgress }}%
      </div>

      <div class="media-error" *ngIf="errorMessage">{{ errorMessage }}</div>
      <div class="media-note" *ngIf="infoMessage">{{ infoMessage }}</div>

      <div class="media-picker">
        <ion-item lines="inset">
          <ion-label>Video file</ion-label>
          <ion-select
            interface="popover"
            [value]="selectedName"
            placeholder="Choose a video"
            (ionChange)="onSelectName($event.detail.value)"
          >
            <ion-select-option *ngFor="let item of items" [value]="item.name">
              {{ item.name }} · {{ formatBytes(item.size_bytes) }}
            </ion-select-option>
          </ion-select>
        </ion-item>
        <div class="media-note" *ngIf="!items.length && !loading">
          No video files in <code>data/files/video</code> yet.
        </div>
      </div>

      <div class="media-meta" *ngIf="selectedItem">
        <div><strong>Name:</strong> {{ selectedItem.name }}</div>
        <div><strong>Size:</strong> {{ formatBytes(selectedItem.size_bytes) }}</div>
        <div><strong>MIME:</strong> {{ selectedItem.mime_type }}</div>
        <div><strong>Updated:</strong> {{ formatDate(selectedItem.modified_at) }}</div>
      </div>

      <div class="media-player" #playerShell>
        <video
          #player
          *ngIf="videoSrc; else playerPlaceholder"
          class="media-player__video"
          [src]="videoSrc"
          controls
          preload="metadata"
        ></video>
        <ng-template #playerPlaceholder>
          <div class="media-player__placeholder">
            <div *ngIf="selectedItem && !canPlaySelected">
              Playback is blocked on the current path. Use a direct local hub connection for larger files.
            </div>
            <div *ngIf="!selectedItem">
              Select a file to preview it.
            </div>
          </div>
        </ng-template>
      </div>

      <div class="media-controls">
        <ion-button size="small" (click)="play()" [disabled]="!videoSrc">Play</ion-button>
        <ion-button size="small" fill="outline" (click)="pause()" [disabled]="!videoSrc">Pause</ion-button>
        <ion-button size="small" fill="outline" (click)="stop()" [disabled]="!videoSrc">Stop</ion-button>
        <ion-button size="small" fill="outline" (click)="fullscreen()" [disabled]="!videoSrc">Fullscreen</ion-button>
        <ion-button
          size="small"
          fill="outline"
          color="danger"
          (click)="deleteSelected()"
          [disabled]="!selectedItem"
        >
          Delete
        </ion-button>
        <ion-button
          size="small"
          fill="outline"
          [href]="videoSrc || null"
          target="_blank"
          rel="noopener"
          [disabled]="!videoSrc"
        >
          Open URL
        </ion-button>
      </div>

      <div class="media-footer" *ngIf="!compact && capabilityNotes.length">
        <div class="media-note" *ngFor="let note of capabilityNotes">{{ note }}</div>
      </div>
    </div>
  `,
  styles: [
    `
      .media-widget {
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .media-widget.compact {
        gap: 10px;
      }
      .media-toolbar {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
      }
      .media-toolbar__title {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .media-toolbar__title h2 {
        margin: 0;
        font-size: 16px;
      }
      .media-toolbar__route {
        font-size: 12px;
        opacity: 0.75;
      }
      .media-toolbar__actions {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
      }
      .media-status {
        display: flex;
        flex-direction: column;
        gap: 8px;
        padding: 12px;
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 12px;
        background: rgba(255, 255, 255, 0.03);
      }
      .media-status__grid {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
      }
      .media-pill {
        padding: 6px 10px;
        border-radius: 999px;
        font-size: 12px;
        line-height: 1.2;
        background: rgba(255, 255, 255, 0.08);
      }
      .media-pill[data-state='ready'] {
        background: rgba(58, 179, 115, 0.18);
      }
      .media-pill[data-state='fallback'] {
        background: rgba(244, 180, 0, 0.18);
      }
      .media-pill[data-state='blocked'],
      .media-pill[data-state='degraded'],
      .media-pill[data-state='unavailable'] {
        background: rgba(224, 67, 54, 0.18);
      }
      .media-note {
        font-size: 12px;
        opacity: 0.82;
      }
      .media-error {
        font-size: 12px;
        color: var(--ion-color-danger, #ff6b6b);
      }
      .media-meta {
        display: grid;
        gap: 4px;
        font-size: 12px;
        opacity: 0.88;
      }
      .media-player {
        position: relative;
        min-height: 220px;
        border-radius: 16px;
        overflow: hidden;
        background: rgba(0, 0, 0, 0.45);
      }
      .media-player__video,
      .media-player__placeholder {
        width: 100%;
        min-height: 220px;
      }
      .media-player__video {
        display: block;
        background: #000;
      }
      .media-player__placeholder {
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 24px;
        text-align: center;
        font-size: 13px;
        opacity: 0.82;
      }
      .media-controls {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
      }
      code {
        font-family: var(--adaos-mono, 'SFMono-Regular', Consolas, monospace);
      }
    `,
  ],
})
export class MediaPlayerWidgetComponent
  implements OnInit, OnChanges, OnDestroy
{
  @Input() widget!: WidgetConfig
  @ViewChild('player') playerRef?: ElementRef<HTMLVideoElement>
  @ViewChild('playerShell') playerShellRef?: ElementRef<HTMLElement>

  items: MediaLibraryItem[] = []
  selectedName = ''
  selectedItem: MediaLibraryItem | null = null
  capabilities: MediaCapabilities = {}
  capabilityNotes: string[] = []
  proxyLimitBytes = 2 * 1024 * 1024
  videoSrc: string | null = null
  loading = false
  uploading = false
  uploadProgress = 0
  uploadName = ''
  errorMessage = ''
  infoMessage = ''
  channelSnapshot?: HubMemberChannelSnapshot

  private uploadSub?: Subscription
  private channelSub?: Subscription

  constructor(
    private http: HttpClient,
    private adaos: AdaosClient,
    private channels: HubMemberChannelsService,
    private modals: PageModalService,
  ) {}

  ngOnInit(): void {
    this.channelSub = this.channels.snapshot$.subscribe((snapshot) => {
      this.channelSnapshot = snapshot
    })
    void this.refreshLibrary()
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['widget'] && !changes['widget'].firstChange) {
      this.syncSelection()
    }
  }

  ngOnDestroy(): void {
    this.uploadSub?.unsubscribe()
    this.channelSub?.unsubscribe()
  }

  get compact(): boolean {
    return !!this.widget?.inputs?.['compact']
  }

  get showDiagnostics(): boolean {
    return !!this.widget?.inputs?.['showDiagnostics']
  }

  get modalId(): string {
    return String(this.widget?.inputs?.['modalId'] || '').trim()
  }

  get isRoutedProxy(): boolean {
    return this.adaos.getBaseUrl().includes('/hubs/')
  }

  get uploadAllowed(): boolean {
    return !this.isRoutedProxy
  }

  get canPlaySelected(): boolean {
    if (!this.selectedItem) return false
    if (!this.isRoutedProxy) return true
    return Number(this.selectedItem.size_bytes || 0) <= this.proxyLimitBytes
  }

  get selectedTooLargeForRouted(): boolean {
    return !!this.selectedItem && this.isRoutedProxy && !this.canPlaySelected
  }

  get routeLabel(): string {
    return this.isRoutedProxy
      ? 'Path: routed root proxy'
      : 'Path: direct local hub'
  }

  get uploadLabel(): string {
    return this.uploadAllowed ? 'direct local ready' : 'disabled on routed path'
  }

  get playbackLabel(): string {
    if (!this.selectedItem) {
      return this.isRoutedProxy ? 'tiny preview only via proxy' : 'direct local ready'
    }
    return this.canPlaySelected ? 'ready' : 'use direct local path'
  }

  get playbackLabelState(): string {
    if (!this.selectedItem) return this.isRoutedProxy ? 'fallback' : 'ready'
    return this.canPlaySelected ? 'ready' : 'blocked'
  }

  get broadcastLabel(): string {
    return 'not implemented'
  }

  get routeHealthLabel(): string {
    const routeHealth = this.channelSnapshot?.channels?.route?.health
    const commandHealth = this.channelSnapshot?.channels?.command?.health
    const syncHealth = this.channelSnapshot?.channels?.sync?.health
    return `route=${routeHealth || 'unknown'}, command=${commandHealth || 'unknown'}, sync=${syncHealth || 'unknown'}`
  }

  get routeHealthState(): string {
    const health = this.channelSnapshot?.channels?.route?.health || 'unavailable'
    if (health === 'ready') return 'ready'
    if (health === 'fallback' || health === 'recovering') return 'fallback'
    return 'degraded'
  }

  async refreshLibrary(): Promise<void> {
    this.loading = true
    this.errorMessage = ''
    this.infoMessage = ''
    try {
      const snapshot = await firstValueFrom(
        this.http.get<MediaLibraryResponse>(this.abs('/api/node/media/files'), {
          headers: this.authHeaders(),
        }),
      )
      this.items = Array.isArray(snapshot?.items) ? snapshot.items : []
      this.capabilities = snapshot?.capabilities || {}
      this.capabilityNotes = Array.isArray(snapshot?.capabilities?.notes)
        ? snapshot.capabilities?.notes || []
        : []
      this.proxyLimitBytes = Number(
        snapshot?.proxy_limits?.root_routed_response_limit_bytes || 2 * 1024 * 1024,
      )
      const keepSelected = this.items.find((item) => item.name === this.selectedName)
      if (keepSelected) {
        this.selectedItem = keepSelected
      } else {
        this.selectedItem = this.items[0] || null
        this.selectedName = this.selectedItem?.name || ''
      }
      this.syncSelection()
    } catch (err: any) {
      this.errorMessage = this.describeError(err)
    } finally {
      this.loading = false
    }
  }

  onSelectName(name: string): void {
    this.selectedName = String(name || '')
    this.selectedItem =
      this.items.find((item) => item.name === this.selectedName) || null
    this.syncSelection()
  }

  onFilePicked(event: Event): void {
    const input = event.target as HTMLInputElement | null
    const file = input?.files?.[0]
    if (input) input.value = ''
    if (!file) return
    void this.uploadFile(file)
  }

  async openModal(): Promise<void> {
    if (!this.modalId) return
    await this.modals.openModalById(this.modalId)
  }

  async deleteSelected(): Promise<void> {
    if (!this.selectedItem) return
    const confirmed = window.confirm(`Delete ${this.selectedItem.name}?`)
    if (!confirmed) return
    this.errorMessage = ''
    this.infoMessage = ''
    try {
      await firstValueFrom(
        this.http.delete(this.abs(`/api/node/media/files/${encodeURIComponent(this.selectedItem.name)}`), {
          headers: this.authHeaders(),
        }),
      )
      this.infoMessage = `Deleted ${this.selectedItem.name}`
      await this.refreshLibrary()
    } catch (err: any) {
      this.errorMessage = this.describeError(err)
    }
  }

  play(): void {
    void this.playerRef?.nativeElement.play().catch(() => {})
  }

  pause(): void {
    this.playerRef?.nativeElement.pause()
  }

  stop(): void {
    const player = this.playerRef?.nativeElement
    if (!player) return
    player.pause()
    try {
      player.currentTime = 0
    } catch {}
  }

  fullscreen(): void {
    const target = this.playerShellRef?.nativeElement || this.playerRef?.nativeElement
    const request = (target as any)?.requestFullscreen
    if (typeof request === 'function') {
      void request.call(target)
    }
  }

  formatBytes(value: number | null | undefined): string {
    const size = Number(value || 0)
    if (!Number.isFinite(size) || size <= 0) return '0 B'
    const units = ['B', 'KB', 'MB', 'GB']
    let index = 0
    let current = size
    while (current >= 1024 && index < units.length - 1) {
      current /= 1024
      index += 1
    }
    const digits = current >= 100 || index === 0 ? 0 : current >= 10 ? 1 : 2
    return `${current.toFixed(digits)} ${units[index]}`
  }

  formatDate(value: string | null | undefined): string {
    if (!value) return 'n/a'
    try {
      return new Date(value).toLocaleString()
    } catch {
      return String(value)
    }
  }

  private syncSelection(): void {
    if (!this.selectedItem) {
      this.videoSrc = null
      return
    }
    if (!this.canPlaySelected) {
      this.stop()
      this.videoSrc = null
      return
    }
    this.videoSrc = this.buildContentUrl(this.selectedItem)
  }

  private async uploadFile(file: File): Promise<void> {
    if (!this.uploadAllowed) {
      this.errorMessage =
        'Upload is disabled on the routed root-proxy path. Use a direct local hub connection for media files.'
      return
    }
    this.uploadSub?.unsubscribe()
    this.uploading = true
    this.uploadName = file.name
    this.uploadProgress = 0
    this.errorMessage = ''
    this.infoMessage = ''

    const headers = this.authHeaders().set(
      'Content-Type',
      file.type || 'application/octet-stream',
    )
    this.uploadSub = this.http
      .put(this.abs(`/api/node/media/files/${encodeURIComponent(file.name)}`), file, {
        headers,
        reportProgress: true,
        observe: 'events',
      })
      .subscribe({
        next: (event: HttpEvent<any>) => {
          if (event.type === HttpEventType.UploadProgress) {
            const total = Number(event.total || 0)
            this.uploadProgress =
              total > 0 ? Math.max(1, Math.round((event.loaded / total) * 100)) : 25
            return
          }
          if (event.type === HttpEventType.Response) {
            this.uploadProgress = 100
            this.infoMessage = `Uploaded ${file.name}`
            void this.refreshLibrary()
          }
        },
        error: (err) => {
          this.errorMessage = this.describeError(err)
          this.uploading = false
        },
        complete: () => {
          this.uploading = false
        },
      })
  }

  private buildContentUrl(item: MediaLibraryItem): string {
    const base = this.adaos.getBaseUrl().replace(/\/$/, '')
    const rel = String(item.content_path || '').startsWith('/')
      ? String(item.content_path)
      : `/api/node/media/files/content/${encodeURIComponent(item.name)}`
    const token = String(this.adaos.getToken() || '').trim()
    if (!token) return `${base}${rel}`
    return `${base}${rel}?token=${encodeURIComponent(token)}`
  }

  private abs(path: string): string {
    const base = this.adaos.getBaseUrl().replace(/\/$/, '')
    const rel = path.startsWith('/') ? path : `/${path}`
    return `${base}${rel}`
  }

  private authHeaders(): HttpHeaders {
    const headers = this.adaos.getAuthHeaders()
    let httpHeaders = new HttpHeaders()
    for (const [key, value] of Object.entries(headers || {})) {
      httpHeaders = httpHeaders.set(key, value)
    }
    return httpHeaders
  }

  private describeError(err: any): string {
    const detail = err?.error?.detail
    if (typeof detail === 'string' && detail.trim()) return detail
    const message = err?.error?.error || err?.message
    if (typeof message === 'string' && message.trim()) return message
    return 'media request failed'
  }
}
