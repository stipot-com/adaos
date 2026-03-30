import { ChangeDetectionStrategy, Component, EventEmitter, Input, OnChanges, OnInit, Output, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicStandaloneImports } from '../../shared/ionic-standalone'
import { Observable, Subscription } from 'rxjs'
import { map } from 'rxjs/operators'
import { PageDataService } from '../../runtime/page-data.service'
import { PageActionService } from '../../runtime/page-action.service'
import { PageStateService } from '../../runtime/page-state.service'
import { WidgetConfig, ActionConfig } from '../../runtime/page-schema.model'
import { PageModalService } from '../../runtime/page-modal.service'
import { isVerboseDebugEnabled } from '../../debug-log'

@Component({
  selector: 'ada-collection-grid-widget',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, IonicStandaloneImports],
  providers: [PageModalService],
  template: `
    <div class="grid-section">
      <h2 *ngIf="widget?.title">{{ widget.title }}</h2>
      <ng-container *ngIf="items$ | async as items">
      <div class="tiles" *ngIf="items.length; else emptyState" [style.--tile-min]="tileMinWidthPx">
        <article
          class="tile"
          *ngFor="let item of items; trackBy: trackByItemId"
          (click)="onItemClick(item)"
          (keydown.enter)="onItemClick(item)"
          (keydown.space)="onTileKeydown($event, item)"
          tabindex="0"
          role="button"
          [class.selected]="isSelected(item)"
          [class.is-skeleton]="item.uiSkeleton"
        >
          <ng-container *ngIf="!item.uiSkeleton; else skeletonTile">
            <ion-badge *ngIf="item.dev" color="warning" class="dev-badge">DEV</ion-badge>
            <div class="tile-badges" *ngIf="item.uiBadges.length">
              <span
                class="tile-badge"
                *ngFor="let badge of item.uiBadges; trackBy: trackByBadge"
                [class.is-active]="badge.tone === 'active'"
                [class.is-accent]="badge.tone === 'accent'"
              >
                {{ badge.label }}
              </span>
            </div>
            <div class="icon-wrapper" *ngIf="item.icon">
              <ion-icon [name]="item.icon"></ion-icon>
            </div>
            <div class="label">{{ item.title || item.id }}</div>
            <div class="subtitle" *ngIf="item.uiSubtitle as subtitle">{{ subtitle }}</div>
            <div class="tile-actions" *ngIf="item.uiQuickActions.length">
              <ion-button
                *ngFor="let btn of item.uiQuickActions; trackBy: trackByAction"
                size="small"
                [color]="btn.color"
                [fill]="btn.fill || 'outline'"
                (click)="onQuickAction(btn.action, item, $event)"
              >
                <ion-icon *ngIf="btn.icon" slot="start" [name]="btn.icon"></ion-icon>
                {{ btn.label }}
              </ion-button>
            </div>
          </ng-container>
          <ng-template #skeletonTile>
            <div class="icon-wrapper skeleton-icon">
              <ion-skeleton-text animated></ion-skeleton-text>
            </div>
            <div class="tile-skeleton-lines">
              <ion-skeleton-text animated style="width: 72%"></ion-skeleton-text>
              <ion-skeleton-text animated style="width: 58%"></ion-skeleton-text>
            </div>
          </ng-template>
        </article>
      </div>
      <ng-template #emptyState>
        <div class="empty-hint">
          <div class="empty-hint__title">No items available yet</div>
          <div class="empty-hint__body">
            This section is empty right now. If this happened right after Yjs reload, the webspace may still be rebuilding.
          </div>
        </div>
      </ng-template>
      </ng-container>
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
        border: 1px solid rgba(255, 255, 255, 0.08);
        background: rgba(255, 255, 255, 0.03);
        color: inherit;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 6px;
        padding: 14px 10px 12px;
        border-radius: 14px;
        transition: background 0.2s, transform 0.15s ease, border-color 0.2s ease;
        min-width: 0;
        cursor: pointer;
        text-align: center;
        outline: none;
      }
      .tile:hover {
        background: rgba(255, 255, 255, 0.06);
        transform: translateY(-1px);
        border-color: rgba(255, 255, 255, 0.14);
      }
      .tile:focus-visible {
        border-color: var(--ion-color-primary, rgba(255, 255, 255, 0.4));
        box-shadow: 0 0 0 2px rgba(var(--ion-color-primary-rgb, 56, 128, 255), 0.24);
      }
      .tile.selected {
        background: rgba(255, 255, 255, 0.12);
        border-color: rgba(255, 255, 255, 0.18);
      }
      .tile.is-skeleton {
        cursor: default;
        pointer-events: none;
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
        font-size: 13px;
        font-weight: 600;
        line-height: 1.25;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
        max-width: 100%;
      }
      .subtitle {
        font-size: 11px;
        opacity: 0.72;
        line-height: 1.3;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
        max-width: 100%;
        min-height: 28px;
      }
      .tile-badges {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
        justify-content: center;
        min-height: 20px;
      }
      .tile-badge {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 2px 8px;
        border-radius: 999px;
        font-size: 10px;
        letter-spacing: 0.02em;
        border: 1px solid rgba(255, 255, 255, 0.08);
        background: rgba(255, 255, 255, 0.05);
        opacity: 0.82;
      }
      .tile-badge.is-active {
        background: rgba(var(--ion-color-success-rgb, 45, 211, 111), 0.14);
        border-color: rgba(var(--ion-color-success-rgb, 45, 211, 111), 0.35);
      }
      .tile-badge.is-accent {
        background: rgba(var(--ion-color-warning-rgb, 255, 196, 9), 0.12);
        border-color: rgba(var(--ion-color-warning-rgb, 255, 196, 9), 0.34);
      }
      .tile-actions {
        display: flex;
        flex-wrap: wrap;
        justify-content: center;
        gap: 6px;
        margin-top: 2px;
      }
      .tile-actions ion-button {
        margin: 0;
      }
      .tile-skeleton-lines {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 8px;
        width: 100%;
      }
      .tile.is-skeleton ion-skeleton-text {
        --background: rgba(255, 255, 255, 0.18);
        --background-rgb: 255, 255, 255;
        width: 100%;
        height: 12px;
        border-radius: 999px;
        margin: 0;
      }
      .tile.is-skeleton .skeleton-icon ion-skeleton-text {
        width: 48px;
        height: 48px;
        border-radius: 16px;
      }
      .dev-badge {
        position: absolute;
        top: 4px;
        right: 4px;
        z-index: 1;
      }
      .empty-hint {
        padding: 16px 14px;
        border-radius: 14px;
        border: 1px dashed rgba(255, 255, 255, 0.12);
        background: rgba(255, 255, 255, 0.03);
      }
      .empty-hint__title {
        font-weight: 600;
        margin-bottom: 6px;
      }
      .empty-hint__body {
        font-size: 12px;
        opacity: 0.8;
        line-height: 1.45;
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
    if (item?.uiSkeleton) return
    item = this.unwrapItem(item)
    this.itemClick.emit(item)
    const cfg = this.widget
    const hasExplicitScenarioSelect = Array.isArray(cfg?.actions)
      && cfg.actions.some(
        (act) =>
          act.on === 'select'
          && act.type === 'callHost'
          && (act.target === 'desktop.scenario.set' || act.target === 'desktop.webspace.ensure_dev')
      )
    if (item?.scenario_id && !hasExplicitScenarioSelect) {
      await this.actions.handle(
        {
          on: 'select',
          type: 'callHost',
          target: 'desktop.scenario.set',
          params: {
            scenario_id: '$event.scenario_id',
            dev: '$event.dev',
            title: '$event.title',
          },
        },
        { event: item, widget: cfg }
      )
    }
    if (!cfg?.actions) return
    for (const act of cfg.actions) {
      if (act.on === 'select') {
        await this.dispatchAction(act, item, cfg)
      }
    }
  }

  onTileKeydown(event: Event, item: any): void {
    event.preventDefault()
    if (item?.uiSkeleton) return
    void this.onItemClick(item)
  }

  private updateItemsStream(): void {
    this.items$ = this.data.load<any[]>(this.widget?.dataSource).pipe(
      map((items) => this.decorateItems(items)),
    )
  }

  isSelected(item: any): boolean {
    item = this.unwrapItem(item)
    const key = this.widget?.inputs?.['selectedStateKey']
    if (!key) return false
    const selected = this.state.get<string>(key)
    if (!selected) return false
    return item?.id === selected || item?.path === selected
  }

  private itemSubtitle(item: any): string {
    return String(item?.subtitle || item?.description || item?.source || item?.origin || '')
      .trim()
  }

  private itemBadges(item: any): Array<{ label: string; tone?: 'active' | 'accent' }> {
    const badges: Array<{ label: string; tone?: 'active' | 'accent' }> = []
    const kindLabel = String(item?.kindLabel || '').trim()
    if (kindLabel) {
      badges.push({ label: kindLabel })
    }
    if (item?.installed) {
      badges.push({ label: 'Installed', tone: 'active' })
    }
    if (item?.pinned) {
      badges.push({ label: 'Pinned', tone: 'accent' })
    }
    return badges
  }

  private quickActionButtons(item: any): Array<{
    action: 'install' | 'pin'
    label: string
    icon?: string
    color?: string
    fill?: 'outline' | 'solid' | 'clear'
  }> {
    const actions: Array<{
      action: 'install' | 'pin'
      label: string
      icon?: string
      color?: string
      fill?: 'outline' | 'solid' | 'clear'
    }> = []
    if (item?.installable && (item?.installType === 'app' || item?.installType === 'widget')) {
      actions.push({
        action: 'install',
        label: item?.installed ? 'Remove' : 'Install',
        icon: item?.installed ? 'close-outline' : 'add-outline',
        color: item?.installed ? 'danger' : 'primary',
        fill: item?.installed ? 'outline' : 'solid',
      })
    }
    if ((item?.pinnable || item?.pinned) && item?.installType === 'widget') {
      actions.push({
        action: 'pin',
        label: item?.pinned ? 'Unpin' : 'Pin',
        icon: 'bookmark-outline',
        color: item?.pinned ? 'warning' : 'medium',
        fill: item?.pinned ? 'solid' : 'outline',
      })
    }
    return actions
  }

  async onQuickAction(action: 'install' | 'pin', item: any, event: Event): Promise<void> {
    event.preventDefault()
    event.stopPropagation()
    if (item?.uiSkeleton) return
    const displayItem = item
    const rawItem = this.unwrapItem(item)
    if (action === 'install') {
      const installType = rawItem?.installType === 'app' ? 'app' : 'widget'
      const ok = await this.actions.toggleDesktopInstall(installType, String(rawItem?.id || ''))
      if (ok) {
        this.applyLocalQuickActionResult(displayItem, rawItem, 'install')
      }
      return
    }
    if (action === 'pin') {
      const ok = await this.actions.toggleDesktopPinnedWidget(rawItem, !rawItem?.pinned)
      if (ok) {
        this.applyLocalQuickActionResult(displayItem, rawItem, 'pin')
      }
    }
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

  trackByItemId = (index: number, item: any): string => {
    const raw = this.unwrapItem(item)
    return String(raw?.id || raw?.path || index)
  }

  trackByBadge = (_index: number, badge: { label: string; tone?: string }): string =>
    `${badge.tone || 'default'}:${badge.label}`

  trackByAction = (_index: number, action: { action: string; label: string }): string =>
    `${action.action}:${action.label}`

  private unwrapItem(item: any): any {
    return item?.uiRaw ?? item
  }

  private decorateItems(items: any[] | undefined): any[] | undefined {
    if (!Array.isArray(items)) return items
    return items.map((item) => {
      const raw = item && typeof item === 'object' ? item : {}
      return {
        ...raw,
        uiRaw: raw,
        uiSubtitle: this.itemSubtitle(raw),
        uiBadges: this.itemBadges(raw),
        uiQuickActions: this.quickActionButtons(raw),
      }
    })
  }

  private applyLocalQuickActionResult(displayItem: any, rawItem: any, action: 'install' | 'pin'): void {
    const targets = new Set<any>()
    if (displayItem && typeof displayItem === 'object') {
      targets.add(displayItem)
    }
    if (rawItem && typeof rawItem === 'object') {
      targets.add(rawItem)
    }
    if (!targets.size) return
    for (const item of targets) {
      this.updateLocalQuickActionTarget(item, action)
    }
    if (displayItem && rawItem && displayItem !== rawItem) {
      displayItem.installType = rawItem.installType
      displayItem.installable = rawItem.installable
      displayItem.installed = rawItem.installed
      displayItem.pinned = rawItem.pinned
      displayItem.pinnable = rawItem.pinnable
      displayItem.uiRaw = rawItem
      displayItem.uiSubtitle = this.itemSubtitle(rawItem)
      displayItem.uiBadges = this.itemBadges(rawItem)
      displayItem.uiQuickActions = this.quickActionButtons(rawItem)
    }
  }

  private updateLocalQuickActionTarget(item: any, action: 'install' | 'pin'): void {
    if (!item || typeof item !== 'object') return
    if (action === 'install') {
      item.installed = !item.installed
      if (item.installType === 'widget') {
        item.pinnable = !!item.installed || !!item.pinned
      }
    } else if (action === 'pin') {
      item.pinned = !item.pinned
      if (item.installType === 'widget') {
        item.pinnable = !!item.installed || !!item.pinned
      }
    }
    item.uiSubtitle = this.itemSubtitle(item)
    item.uiBadges = this.itemBadges(item)
    item.uiQuickActions = this.quickActionButtons(item)
  }
}
