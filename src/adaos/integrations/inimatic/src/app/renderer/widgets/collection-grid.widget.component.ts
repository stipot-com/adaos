import { Component, EventEmitter, Input, OnChanges, OnInit, Output, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { Observable, Subscription } from 'rxjs'
import { PageDataService } from '../../runtime/page-data.service'
import { PageActionService } from '../../runtime/page-action.service'
import { PageStateService } from '../../runtime/page-state.service'
import { WidgetConfig, ActionConfig } from '../../runtime/page-schema.model'
import { PageModalService } from '../../runtime/page-modal.service'

@Component({
  selector: 'ada-collection-grid-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  providers: [PageModalService],
  template: `
    <div class="grid-section">
      <h2 *ngIf="widget?.title">{{ widget.title }}</h2>
      <ion-grid *ngIf="items$ | async as items">
        <ion-row>
          <ion-col
            *ngFor="let item of items"
            [size]="columnSize"
            (click)="onItemClick(item)"
            class="collection-grid-item"
            [class.selected]="isSelected(item)"
          >
            <ion-badge *ngIf="item.dev" color="warning" class="dev-badge">DEV</ion-badge>
            <button class="icon-button">
              <div class="icon-wrapper" *ngIf="item.icon">
                <ion-icon [name]="item.icon"></ion-icon>
              </div>
              <div class="label">{{ item.title || item.id }}</div>
            </button>
          </ion-col>
        </ion-row>
      </ion-grid>
    </div>
  `,
  styles: [
    `
      .grid-section {
        padding: 8px 0;
      }
      .grid-section h2 {
        font-size: 14px;
        font-weight: 500;
        margin: 0 0 8px;
        text-transform: uppercase;
      }
      .collection-grid-item {
        text-align: center;
        padding: 8px 0;
      }
      .collection-grid-item.selected .icon-button {
        background: rgba(255, 255, 255, 0.12);
      }
      .icon-button {
        width: 100%;
        border: none;
        background: transparent;
        color: inherit;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 6px;
        padding: 12px 4px;
        border-radius: 12px;
        transition: background 0.2s;
      }
      .icon-button:hover {
        background: rgba(255, 255, 255, 0.06);
      }
      .icon-wrapper {
        font-size: 32px;
        margin-bottom: 4px;
      }
      .icon-wrapper ion-icon {
        width: 48px;
        height: 48px;
      }
      .label {
        font-size: 12px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .dev-badge {
        position: absolute;
        top: 4px;
        right: 4px;
        z-index: 1;
      }
    `,
  ],
})
export class CollectionGridWidgetComponent implements OnInit, OnChanges {
  @Input() widget!: WidgetConfig
  @Output() itemClick = new EventEmitter<any>()

  items$?: Observable<any[] | undefined>
  private stateSub?: Subscription
  private lastState: Record<string, any> = {}
  private stateDeps: string[] = []

  constructor(
    private data: PageDataService,
    private actions: PageActionService,
    private state: PageStateService,
    private modals: PageModalService
  ) {}

  ngOnInit(): void {
    this.recomputeStateDeps()
    this.updateItemsStream()
    this.stateSub = this.state.selectAll().subscribe(() => {
      this.onStateChanged()
    })
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['widget']) {
      this.recomputeStateDeps()
      this.updateItemsStream()
    }
  }

  ngOnDestroy(): void {
    this.stateSub?.unsubscribe()
  }

  get columnSize(): string {
    const columns = Number(this.widget?.inputs?.['columns'] ?? 4)
    const value = !columns || columns <= 0 ? 4 : Math.max(1, Math.min(12, Math.floor(12 / columns)))
    return String(value)
  }

  async onItemClick(item: any): Promise<void> {
    try {
      // eslint-disable-next-line no-console
      console.log('[CollectionGridWidget] onItemClick', this.widget?.id, item)
    } catch {}
    this.itemClick.emit(item)
    const cfg = this.widget
    if (!cfg?.actions) return
    for (const act of cfg.actions) {
      if (act.on === 'select') {
        await this.dispatchAction(act, item, cfg)
      }
    }
  }

  private updateItemsStream(): void {
    this.items$ = this.data.load<any[]>(this.widget?.dataSource)
    try {
      // eslint-disable-next-line no-console
      console.log('[CollectionGridWidget] updateItemsStream', this.widget?.id, 'dataSource=', this.widget?.dataSource)
    } catch {}
  }

  isSelected(item: any): boolean {
    const key = this.widget?.inputs?.['selectedStateKey']
    if (!key) return false
    const selected = this.state.get<string>(key)
    if (!selected) return false
    return item?.id === selected || item?.path === selected
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
        this.updateItemsStream()
        break
      }
    }
  }

  private async dispatchAction(act: ActionConfig, item: any, widget: WidgetConfig): Promise<void> {
    if (act.type === 'openModal') {
      const modalId = this.resolveValue(act.params?.['modalId'], item)
      await this.modals.openModalById(modalId)
      return
    }
    await this.actions.handle(act, { event: item, widget })
  }

  private resolveValue(value: any, event: any): any {
    if (typeof value !== 'string') return value
    if (value.startsWith('$event.')) {
      const path = value.slice('$event.'.length)
      return path.split('.').reduce((acc, key) => (acc != null ? acc[key] : undefined), event)
    }
    return value
  }
}
