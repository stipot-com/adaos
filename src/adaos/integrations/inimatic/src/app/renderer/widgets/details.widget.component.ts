import { Component, Input, OnChanges, OnDestroy, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { Observable, Subscription } from 'rxjs'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { PageStateService } from '../../runtime/page-state.service'

@Component({
  selector: 'ada-details-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `
    <ion-card>
      <ion-card-header *ngIf="widget?.title">
        <ion-card-title>{{ widget.title }}</ion-card-title>
      </ion-card-header>
      <ion-card-content>
        <ng-container *ngIf="data$ | async as value">
          <div class="details-notice" *ngIf="readNotice(value) as notice">{{ notice }}</div>
          <pre>{{ value | json }}</pre>
        </ng-container>
      </ion-card-content>
    </ion-card>
  `,
  styles: [
    `
      .details-notice {
        margin-bottom: 10px;
        padding: 8px 10px;
        border-radius: 10px;
        font-size: 12px;
        line-height: 1.4;
        background: rgba(var(--ion-color-warning-rgb, 255, 196, 9), 0.12);
        border: 1px solid rgba(var(--ion-color-warning-rgb, 255, 196, 9), 0.26);
      }
      pre {
        white-space: pre-wrap;
        word-break: break-word;
        user-select: text;
      }
    `,
  ],
})
export class DetailsWidgetComponent implements OnInit, OnChanges, OnDestroy {
  @Input() widget!: WidgetConfig

  data$?: Observable<any>
  private stateSub?: Subscription
  private stateDeps: string[] = []
  private lastState: Record<string, any> = {}

  constructor(
    private data: PageDataService,
    private state: PageStateService
  ) {}

  ngOnInit(): void {
    this.recomputeStateDeps()
    this.updateStream()
    this.stateSub = this.state.selectAll().subscribe(() => {
      this.onStateChanged()
    })
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.updateStream()
  }

  ngOnDestroy(): void {
    this.stateSub?.unsubscribe()
  }

  private updateStream(): void {
    const ds = this.widget?.dataSource
    const bindField: string =
      (this.widget.inputs && this.widget.inputs['bindField']) || ''
    if (!ds) {
      this.data$ = undefined
      return
    }
    if (!bindField) {
      this.data$ = this.data.load<any>(ds)
      return
    }
    this.data$ = this.data.load<any>(ds).pipe(
      (source) =>
        new Observable<any>((subscriber) => {
          const sub = source.subscribe({
            next: (value) => {
              try {
                const next =
                  value && typeof value === 'object' ? (value as any)[bindField] : undefined
                subscriber.next(next)
              } catch {
                subscriber.next(undefined)
              }
            },
            error: (err) => subscriber.error(err),
            complete: () => subscriber.complete(),
          })
          return () => sub.unsubscribe()
        }),
    )
  }

  private recomputeStateDeps(): void {
    this.stateDeps = []
    const params = this.widget?.dataSource && (this.widget.dataSource as any).params
    if (!params || typeof params !== 'object') return
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

  readNotice(value: any): string {
    const warning = typeof value?.warning === 'string' ? value.warning.trim() : ''
    const error = typeof value?.error === 'string' ? value.error.trim() : ''
    return warning || error || ''
  }
}
