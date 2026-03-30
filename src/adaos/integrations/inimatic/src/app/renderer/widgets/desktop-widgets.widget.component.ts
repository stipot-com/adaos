import { Component, Input, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicStandaloneImports } from '../../shared/ionic-standalone'
import { WidgetConfig, WidgetType } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { Subscription } from 'rxjs'
import { MetricTileWidgetComponent } from './metric-tile.widget.component'
import { CommandBarWidgetComponent } from './command-bar.widget.component'
import { PageActionService } from '../../runtime/page-action.service'

@Component({
  selector: 'ada-desktop-widgets',
  standalone: true,
  imports: [CommonModule, IonicStandaloneImports, MetricTileWidgetComponent, CommandBarWidgetComponent],
  template: `
    <div class="widgets-section">
      <div class="widgets-header">
        <h2 *ngIf="widget?.title">{{ widget.title }}</h2>
        <div class="widgets-summary" *ngIf="widgets.length">
          {{ widgets.length }} active
        </div>
      </div>
      <ng-container *ngIf="widgets.length; else emptyState">
        <div class="widgets-grid">
          <div class="widget-wrapper" *ngFor="let w of widgets">
            <ion-badge *ngIf="w.inputs?.['dev']" color="warning" class="dev-badge">DEV</ion-badge>
            <div class="widget-shell">
              <div class="widget-toolbar">
                <div class="widget-toolbar__copy">
                  <div class="widget-toolbar__title">{{ widgetCardTitle(w) }}</div>
                  <div class="widget-toolbar__subtitle" *ngIf="widgetCardSubtitle(w) as subtitle">
                    {{ subtitle }}
                  </div>
                </div>
                <div class="widget-toolbar__actions">
                  <span class="widget-chip" *ngIf="w.inputs?.['installed']">Installed</span>
                  <span class="widget-chip widget-chip--accent" *ngIf="w.inputs?.['pinned']">Pinned</span>
                  <ion-button
                    *ngIf="canTogglePin(w)"
                    size="small"
                    fill="clear"
                    color="medium"
                    (click)="onTogglePin(w, $event)"
                  >
                    {{ w.inputs?.['pinned'] ? 'Unpin' : 'Pin' }}
                  </ion-button>
                  <ion-button
                    *ngIf="canRemove(w)"
                    size="small"
                    fill="clear"
                    color="danger"
                    (click)="onToggleInstall(w, $event)"
                  >
                    Remove
                  </ion-button>
                </div>
              </div>
              <ng-container [ngSwitch]="w.type">
                <ada-metric-tile-widget
                  *ngSwitchCase="'visual.metricTile'"
                  [widget]="w"
                ></ada-metric-tile-widget>
                <ada-command-bar-widget
                  *ngSwitchCase="'input.commandBar'"
                  [widget]="w"
                ></ada-command-bar-widget>
                <ada-metric-tile-widget
                  *ngSwitchDefault
                  [widget]="w"
                ></ada-metric-tile-widget>
              </ng-container>
            </div>
          </div>
        </div>
      </ng-container>
      <ng-template #emptyState>
        <div class="empty-hint">
          <div class="empty-hint__title">No widgets on this desktop yet</div>
          <div class="empty-hint__copy">
            Install a widget from the catalog, then pin the ones you want to keep visible.
          </div>
        </div>
      </ng-template>
    </div>
  `,
  styles: [
    `
      .widgets-section {
        padding: 8px 0;
      }
      .widgets-header {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 8px;
      }
      .widgets-section h2 {
        font-size: 14px;
        font-weight: 500;
        margin: 0;
        text-transform: uppercase;
      }
      .widgets-summary {
        font-size: 11px;
        opacity: 0.72;
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .widgets-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 12px;
        align-items: start;
      }
      .widget-wrapper {
        position: relative;
        min-width: 0;
      }
      .widget-shell {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .widget-toolbar {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 10px;
        padding: 10px 12px;
        border-radius: 14px;
        border: 1px solid rgba(255, 255, 255, 0.08);
        background: rgba(255, 255, 255, 0.04);
      }
      .widget-toolbar__copy {
        min-width: 0;
        flex: 1 1 auto;
      }
      .widget-toolbar__title {
        font-size: 13px;
        font-weight: 600;
        line-height: 1.25;
      }
      .widget-toolbar__subtitle {
        margin-top: 4px;
        font-size: 11px;
        opacity: 0.72;
        line-height: 1.35;
        overflow-wrap: anywhere;
      }
      .widget-toolbar__actions {
        display: flex;
        flex-wrap: wrap;
        justify-content: flex-end;
        gap: 6px;
        align-items: center;
      }
      .widget-chip {
        display: inline-flex;
        align-items: center;
        padding: 2px 8px;
        border-radius: 999px;
        font-size: 10px;
        border: 1px solid rgba(255, 255, 255, 0.08);
        background: rgba(var(--ion-color-success-rgb, 45, 211, 111), 0.12);
      }
      .widget-chip--accent {
        background: rgba(var(--ion-color-warning-rgb, 255, 196, 9), 0.12);
      }
      .dev-badge {
        position: absolute;
        top: 4px;
        right: 4px;
        z-index: 1;
      }
      .empty-hint {
        color: var(--ion-color-medium);
        font-size: 14px;
        padding: 12px 0;
      }
      .empty-hint__title {
        font-size: 14px;
        font-weight: 600;
        color: inherit;
      }
      .empty-hint__copy {
        margin-top: 6px;
        font-size: 12px;
        opacity: 0.78;
      }
    `,
  ],
})
export class DesktopWidgetsWidgetComponent implements OnInit, OnDestroy {
  @Input() widget!: WidgetConfig

  widgets: Array<WidgetConfig> = []

  private dataSub?: Subscription

  constructor(
    private data: PageDataService,
    private actions: PageActionService,
  ) {}

  ngOnInit(): void {
    const stream = this.data.load<any[]>(this.widget?.dataSource)
    if (stream) {
      this.dataSub = stream.subscribe((items) => {
        const raw = Array.isArray(items) ? items : []
        this.widgets = raw.map((it) => {
          const cfg: any = { ...(it && typeof it === 'object' ? it : {}) }
          cfg.id = String(cfg.id || '')
          cfg.type = String(cfg.type || 'visual.metricTile') as WidgetType
          cfg.area = this.widget.area
          if (!cfg.inputs || typeof cfg.inputs !== 'object') cfg.inputs = {}
          cfg.inputs = {
            ...cfg.inputs,
            dev: !!cfg.dev,
            installed: !!cfg.installed,
            pinned: !!cfg.pinned,
          }
          // Only treat `source` as a Yjs path when it actually looks like one.
          // `source` can also be provenance (e.g. "skill:voice_chat_skill").
          const source = typeof cfg.source === 'string' ? String(cfg.source) : ''
          const looksLikeYPath = source.startsWith('y:') || source.startsWith('data/')
          if (!cfg.dataSource && looksLikeYPath) {
            cfg.dataSource = {
              kind: 'y',
              path: source.startsWith('y:') ? source.slice(2) : source,
            }
          }
          return cfg as WidgetConfig
        })
      })
    }
  }

  ngOnDestroy(): void {
    this.dataSub?.unsubscribe()
  }

  widgetCardTitle(widget: WidgetConfig): string {
    return String(widget?.title || widget?.id || 'Widget')
  }

  widgetCardSubtitle(widget: WidgetConfig): string {
    const anyWidget: any = widget as any
    return String(anyWidget?.subtitle || anyWidget?.source || anyWidget?.origin || '').trim()
  }

  canTogglePin(widget: WidgetConfig): boolean {
    return !!widget?.inputs?.['installed'] || !!widget?.inputs?.['pinned']
  }

  canRemove(widget: WidgetConfig): boolean {
    return !!widget?.inputs?.['installed']
  }

  async onTogglePin(widget: WidgetConfig, event: Event): Promise<void> {
    event.preventDefault()
    event.stopPropagation()
    await this.actions.toggleDesktopPinnedWidget(widget as any, !widget?.inputs?.['pinned'])
  }

  async onToggleInstall(widget: WidgetConfig, event: Event): Promise<void> {
    event.preventDefault()
    event.stopPropagation()
    await this.actions.toggleDesktopInstall('widget', String(widget?.id || ''))
  }
}
