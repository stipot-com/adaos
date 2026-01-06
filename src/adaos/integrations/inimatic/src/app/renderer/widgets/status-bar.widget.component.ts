import { Component, Input, OnChanges, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { Observable } from 'rxjs'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'

@Component({
  selector: 'ada-status-bar-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `
    <div class="status-bar" *ngIf="message$ | async as message">
      <span class="label">{{ widget.title || 'Status' }}</span>
      <span class="message">{{ message }}</span>
    </div>
  `,
  styles: [
    `
      .status-bar {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 10px;
        font-size: 13px;
        border-radius: 8px;
        background: var(--ion-background-color);
        color: var(--ion-text-color);
        border: 1px solid rgba(var(--ion-text-color-rgb), 0.08);
      }
      .label {
        font-weight: 500;
        opacity: 0.8;
      }
      .message {
        flex: 1;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
    `,
  ],
})
export class StatusBarWidgetComponent implements OnInit, OnChanges {
  @Input() widget!: WidgetConfig

  message$?: Observable<string | undefined>

  constructor(private data: PageDataService) {}

  ngOnInit(): void {
    this.updateStream()
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.updateStream()
  }

  private updateStream(): void {
    const ds = this.widget?.dataSource
    if (!ds) {
      this.message$ = undefined
      return
    }
    this.message$ = this.data.load<any>(ds).pipe(
      (source) =>
        new Observable<string | undefined>((subscriber) => {
          const sub = source.subscribe({
            next: (value) => {
              try {
                const msg =
                  value && typeof value === 'object'
                    ? (value as any)['message']
                    : undefined
                subscriber.next(typeof msg === 'string' ? msg : undefined)
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
}
