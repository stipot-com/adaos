import { Component, Input, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { YDocService } from '../../y/ydoc.service'
import { Observable, Subscription } from 'rxjs'
import { WeatherWidgetComponent } from './weather-widget.component'

@Component({
  selector: 'ada-desktop-widgets',
  standalone: true,
  imports: [CommonModule, IonicModule, WeatherWidgetComponent],
  template: `
    <div class="widgets-section">
      <h2 *ngIf="widget?.title">{{ widget.title }}</h2>
      <ng-container *ngIf="weatherMeta; else emptyState">
        <div class="widget-wrapper">
          <ion-badge *ngIf="weatherMeta?.dev" color="warning" class="dev-badge">DEV</ion-badge>
          <ada-weather-widget
            [title]="weatherMeta!.title || weatherMeta!.id || ''"
            [data]="weatherData$ | async"
          ></ada-weather-widget>
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
      .widget-wrapper {
        position: relative;
        margin-bottom: 8px;
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
      }
    `,
  ],
})
export class DesktopWidgetsWidgetComponent implements OnInit, OnDestroy {
  @Input() widget!: WidgetConfig

  widgets: Array<{ id: string; type: string; title?: string; source?: string; dev?: boolean }> = []
  weatherMeta?: { id: string; type: string; title?: string; source?: string; dev?: boolean }
  weatherData$?: Observable<any | undefined>

  private dataSub?: Subscription

  constructor(private data: PageDataService, private ydoc: YDocService) {}

  ngOnInit(): void {
    const stream = this.data.load<any[]>(this.widget?.dataSource)
    if (stream) {
      this.dataSub = stream.subscribe((items) => {
        this.widgets = Array.isArray(items) ? items : []
        this.weatherMeta =
          this.widgets.find((w) => w.id === 'weather' || w.type === 'weather') ||
          undefined
        if (this.weatherMeta) {
          // Для web_desktop источник погоды фиксирован: data/weather/current.
          this.weatherData$ = this.data.load<any>({
            kind: 'y',
            path: 'data/weather/current',
          } as any)
        } else {
          this.weatherData$ = undefined
        }
      })
    }
  }

  ngOnDestroy(): void {
    this.dataSub?.unsubscribe()
  }

  get weatherData(): any {
    return this.weatherData$
  }
}
