import { Component, Input, OnChanges, OnDestroy, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { Observable, Subscription } from 'rxjs'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { PageActionService } from '../../runtime/page-action.service'
import { PageStateService } from '../../runtime/page-state.service'

@Component({
  selector: 'ada-metric-tile-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `
    <ion-card (click)="onClick()">
      <ion-card-header>
        <ion-card-title>{{ widget.title }}</ion-card-title>
        <ion-card-subtitle *ngIf="(data$ | async)?.subtitle as subtitle">
          {{ subtitle }}
        </ion-card-subtitle>
      </ion-card-header>
      <ion-card-content *ngIf="data$ | async as data">
        <div class="metric-main">
          {{ data?.value ?? data?.temp_c ?? '--' }}
        </div>
        <div class="metric-sub" *ngIf="data?.label || data?.city">
          {{ data.label || data.city }}
        </div>
        <div class="metric-desc" *ngIf="data?.description">
          {{ data.description }}
        </div>
        <div class="metric-notice" *ngIf="data?.warning || data?.error">
          {{ data.warning || data.error }}
        </div>
        <div class="metric-actions" *ngIf="buttonItems(data).length">
          <ion-button
            *ngFor="let btn of buttonItems(data)"
            size="small"
            [color]="btn?.kind === 'danger' ? 'danger' : 'primary'"
            [fill]="btn?.kind === 'danger' ? 'solid' : 'outline'"
            (click)="onButton(btn, $event)"
          >
            {{ btn?.label || btn?.title || btn?.id }}
          </ion-button>
        </div>
      </ion-card-content>
    </ion-card>
  `,
  styles: [
    `
      .metric-main {
        font-size: 32px;
        line-height: 1.1;
      }
      .metric-sub {
        font-size: 14px;
        opacity: 0.8;
      }
      .metric-desc {
        margin-top: 8px;
        font-size: 13px;
        opacity: 0.85;
      }
      .metric-actions {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-top: 12px;
      }
      .metric-notice {
        margin-top: 8px;
        font-size: 12px;
        line-height: 1.4;
        padding: 8px 10px;
        border-radius: 10px;
        background: rgba(var(--ion-color-warning-rgb, 255, 196, 9), 0.12);
        border: 1px solid rgba(var(--ion-color-warning-rgb, 255, 196, 9), 0.26);
      }
    `,
  ],
})
export class MetricTileWidgetComponent implements OnInit, OnChanges, OnDestroy {
  @Input() widget!: WidgetConfig

  data$?: Observable<any>
  private stateSub?: Subscription
  private stateDeps: string[] = []
  private lastState: Record<string, any> = {}

  constructor(
    private data: PageDataService,
    private actions: PageActionService,
    private state: PageStateService,
  ) {}

  ngOnInit(): void {
    this.recomputeStateDeps()
    this.updateStream()
    this.stateSub = this.state.selectAll().subscribe(() => this.onStateChanged())
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['widget']) {
      this.recomputeStateDeps()
      this.updateStream()
    }
  }

  ngOnDestroy(): void {
    this.stateSub?.unsubscribe()
  }

  async onClick(): Promise<void> {
    const cfg = this.widget
    if (!cfg?.actions) return
    for (const act of cfg.actions) {
      if (act.on === 'click' || act.on === 'click:weather') {
        await this.actions.handle(act, { widget: cfg })
      }
    }
  }

  buttonItems(data: any): Array<{ id: string; label?: string; title?: string; kind?: string }> {
    const raw = data?.buttons
    return Array.isArray(raw) ? raw : []
  }

  async onButton(btn: any, event: Event): Promise<void> {
    event.stopPropagation()
    const cfg = this.widget
    if (!cfg?.actions) return
    const payload = { ...(btn || {}), ts: Date.now() }
    const clickId = `click:${payload.id || ''}`
    for (const act of cfg.actions) {
      if (act.on === clickId || act.on === 'click') {
        await this.actions.handle(act, { event: payload, widget: cfg })
      }
    }
  }

  private updateStream(): void {
    this.data$ = this.data.load<any>(this.widget?.dataSource)
  }

  private recomputeStateDeps(): void {
    this.stateDeps = []
    const params = this.widget?.dataSource && (this.widget.dataSource as any).params
    if (!params || typeof params !== 'object') {
      this.lastState = this.state.getSnapshot()
      return
    }
    for (const value of Object.values(params)) {
      if (typeof value === 'string' && value.startsWith('$state.')) {
        const key = value.slice('$state.'.length)
        if (key && !this.stateDeps.includes(key)) {
          this.stateDeps.push(key)
        }
      }
    }
    this.lastState = this.state.getSnapshot()
  }

  private onStateChanged(): void {
    if (!this.stateDeps.length) return
    const next = this.state.getSnapshot()
    const prev = this.lastState
    this.lastState = next
    for (const key of this.stateDeps) {
      if (prev[key] !== next[key]) {
        this.updateStream()
        break
      }
    }
  }
}
