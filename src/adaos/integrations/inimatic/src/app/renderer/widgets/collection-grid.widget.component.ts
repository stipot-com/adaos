import { Component, EventEmitter, Input, OnChanges, OnInit, Output, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { Observable, Subscription } from 'rxjs'
import { PageDataService } from '../../runtime/page-data.service'
import { PageActionService } from '../../runtime/page-action.service'
import { PageStateService } from '../../runtime/page-state.service'
import { WidgetConfig, ActionConfig } from '../../runtime/page-schema.model'
import { PageModalService } from '../../runtime/page-modal.service'
import { isVerboseDebugEnabled } from '../../debug-log'

@Component({
  selector: 'ada-collection-grid-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  providers: [PageModalService],
  template: `
    <div class="grid-section">
      <h2 *ngIf="widget?.title">{{ widget.title }}</h2>
      <div class="tiles" *ngIf="items$ | async as items" [style.--tile-min]="tileMinWidthPx">
        <button
          type="button"
          class="tile"
          *ngFor="let item of items"
          (click)="onItemClick(item)"
          [class.selected]="isSelected(item)"
        >
          <ion-badge *ngIf="item.dev" color="warning" class="dev-badge">DEV</ion-badge>
          <div class="icon-wrapper" *ngIf="item.icon">
            <ion-icon [name]="item.icon"></ion-icon>
          </div>
          <div class="label">{{ item.title || item.id }}</div>
        </button>
      </div>
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
      .tiles {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(var(--tile-min), 1fr));
        gap: 12px;
        align-items: start;
      }
      .tile {
        position: relative;
        border: none;
        background: transparent;
        color: inherit;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 6px;
        padding: 14px 8px;
        border-radius: 14px;
        transition: background 0.2s, transform 0.15s ease;
        min-width: 0;
      }
      .tile:hover {
        background: rgba(255, 255, 255, 0.06);
        transform: translateY(-1px);
      }
      .tile.selected {
        background: rgba(255, 255, 255, 0.12);
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
        max-width: 100%;
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

  get tileMinWidthPx(): string {
    const raw = Number(this.widget?.inputs?.['tileMinWidth'] ?? 96)
    const value = !raw || raw < 72 ? 72 : Math.min(180, Math.floor(raw))
    return `${value}px`
  }

  async onItemClick(item: any): Promise<void> {
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
