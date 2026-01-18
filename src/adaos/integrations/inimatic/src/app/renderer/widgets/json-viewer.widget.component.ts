import { CommonModule } from '@angular/common'
import { Component, Input, OnChanges, OnDestroy, OnInit, SimpleChanges } from '@angular/core'
import { IonicModule } from '@ionic/angular'
import { Observable, Subscription, of } from 'rxjs'
import { PageDataService } from '../../runtime/page-data.service'
import { PageStateService } from '../../runtime/page-state.service'
import { WidgetConfig } from '../../runtime/page-schema.model'

@Component({
  selector: 'ada-json-viewer-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `
    <div class="json-viewer">
      <h2 *ngIf="widget?.title">{{ widget.title }}</h2>

      <ng-container *ngIf="value$ | async as value">
        <pre class="pre" *ngIf="value != null; else empty">{{ format(value) }}</pre>
      </ng-container>

      <ng-template #empty>
        <div class="empty">{{ emptyText }}</div>
      </ng-template>
    </div>
  `,
  styles: [
    `
      .json-viewer h2 {
        font-size: 14px;
        font-weight: 500;
        margin: 0 0 8px;
        text-transform: uppercase;
      }
      .pre {
        white-space: pre-wrap;
        word-break: break-word;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
        font-size: 12px;
        padding: 10px;
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.06);
        margin: 0;
      }
      .empty {
        opacity: 0.7;
        padding: 8px 0;
      }
    `,
  ],
})
export class JsonViewerWidgetComponent implements OnInit, OnChanges, OnDestroy {
  @Input() widget!: WidgetConfig

  value$?: Observable<any>
  private stateSub?: Subscription

  emptyText = 'No data'

  constructor(
    private data: PageDataService,
    private state: PageStateService,
  ) {}

  ngOnInit(): void {
    this.updateSource()
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.updateSource()
  }

  ngOnDestroy(): void {
    this.stateSub?.unsubscribe()
  }

  private updateSource(): void {
    this.stateSub?.unsubscribe()
    const inputs: any = this.widget?.inputs || {}
    this.emptyText = typeof inputs.emptyText === 'string' ? inputs.emptyText : 'No data'

    const stateKey = typeof inputs.stateKey === 'string' ? inputs.stateKey : ''
    if (stateKey) {
      this.value$ = this.state.select(stateKey)
      return
    }

    this.value$ = this.widget?.dataSource ? this.data.load<any>(this.widget.dataSource) : of(undefined)
  }

  format(value: any): string {
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return String(value)
    }
  }
}
