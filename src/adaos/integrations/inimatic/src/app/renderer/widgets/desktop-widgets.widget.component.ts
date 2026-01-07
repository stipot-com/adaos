import { Component, Input, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { WidgetConfig, WidgetType } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { Subscription } from 'rxjs'
import { MetricTileWidgetComponent } from './metric-tile.widget.component'
import { PageWidgetHostComponent } from './page-widget-host.component'

@Component({
  selector: 'ada-desktop-widgets',
  standalone: true,
  imports: [CommonModule, IonicModule, MetricTileWidgetComponent, PageWidgetHostComponent],
  template: `
    <div class="widgets-section">
      <h2 *ngIf="widget?.title">{{ widget.title }}</h2>
      <ng-container *ngIf="widgets.length; else emptyState">
        <div class="widgets-grid">
          <div class="widget-wrapper" *ngFor="let w of widgets">
            <ion-badge *ngIf="w.inputs?.['dev']" color="warning" class="dev-badge">DEV</ion-badge>
            <ng-container [ngSwitch]="w.type">
              <ada-metric-tile-widget
                *ngSwitchCase="'visual.metricTile'"
                [widget]="w"
              ></ada-metric-tile-widget>
              <ion-card *ngSwitchDefault class="widget-card">
                <ion-card-header *ngIf="w.title">
                  <ion-card-title>{{ w.title }}</ion-card-title>
                </ion-card-header>
                <ion-card-content>
                  <ada-page-widget-host [widget]="w"></ada-page-widget-host>
                </ion-card-content>
              </ion-card>
            </ng-container>
          </div>
        </div>
      </ng-container>
      <ng-template #emptyState>
        <div class="empty-hint">No widgets installed</div>
      </ng-template>
    </div>
  `,
  styles: [
    `
      .widgets-section {
        padding: 8px 0;
      }
      .widgets-section h2 {
        font-size: 14px;
        font-weight: 500;
        margin: 0 0 8px;
        text-transform: uppercase;
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
      .dev-badge {
        position: absolute;
        top: 4px;
        right: 4px;
        z-index: 1;
      }
      .widget-card {
        margin: 0;
      }
      .empty-hint {
        color: var(--ion-color-medium);
        font-size: 14px;
      }
    `,
  ],
})
export class DesktopWidgetsWidgetComponent implements OnInit, OnDestroy {
  @Input() widget!: WidgetConfig

  widgets: Array<WidgetConfig> = []

  private dataSub?: Subscription

  constructor(private data: PageDataService) {}

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
          cfg.inputs = { ...cfg.inputs, dev: !!cfg.dev }
          if (!cfg.dataSource && cfg.source) {
            cfg.dataSource = {
              kind: 'y',
              path: String(cfg.source).startsWith('y:') ? String(cfg.source).slice(2) : String(cfg.source),
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
}
