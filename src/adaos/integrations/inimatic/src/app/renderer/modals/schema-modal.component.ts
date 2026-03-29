// src\adaos\integrations\inimatic\src\app\renderer\modals\schema-modal.component.ts
import { ChangeDetectionStrategy, Component, Input, OnChanges, OnDestroy, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { ModalController } from '@ionic/angular/standalone'
import { addIcons } from 'ionicons'
import { Observable } from 'rxjs'
import { map } from 'rxjs/operators'
import { PageSchema, WidgetConfig, ActionConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { PageActionService } from '../../runtime/page-action.service'
import { PageWidgetHostComponent } from '../widgets/page-widget-host.component'
import { MetricTileWidgetComponent } from '../widgets/metric-tile.widget.component'
import { SelectorWidgetComponent } from '../widgets/selector.widget.component'
import { TextInputWidgetComponent } from '../widgets/text-input.widget.component'
import { CommandBarWidgetComponent } from '../widgets/command-bar.widget.component'
import { TextEditorWidgetComponent } from '../widgets/text-editor.widget.component'
import { DetailsWidgetComponent } from '../widgets/details.widget.component'
import { ChatWidgetComponent } from '../widgets/chat.widget.component'
import { VoiceInputWidgetComponent } from '../widgets/voice-input.widget.component'
import { PageStateService } from '../../runtime/page-state.service'
import { contractOutline, expandOutline } from 'ionicons/icons'

@Component({
  selector: 'ada-schema-collection-grid',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, IonicModule],
  template: `
    <div class="grid-section" *ngIf="widget">
      <h2 *ngIf="widget.title">{{ widget.title }}</h2>

      <ng-container *ngIf="items$ | async as items">
        <ng-container *ngIf="items.length; else emptyState">
        <!-- Dev projects: simple vertical list with one item per project -->
        <ng-container *ngIf="isProjectSelector; else standardGrid">
          <ion-list>
            <ion-item
              *ngFor="let item of items"
              lines="full"
              button
              (click)="onItemClick(item)"
            >
              <ion-label>
                <div class="project-name">
                  {{ item.title || item.name || item.object_id || item.id }}
                </div>
                <div class="project-type">
                  {{ item.object_type || item.type }}
                  <span *ngIf="item.version">&nbsp;· v{{ item.version }}</span>
                </div>
                <div class="project-description" *ngIf="item.description">
                  {{ item.description }}
                </div>
                <div class="project-updated" *ngIf="item.updated_at">
                  Updated: {{ item.updated_at }}
                </div>
              </ion-label>
            </ion-item>
          </ion-list>
        </ng-container>

        <!-- Default layout for apps/widgets catalogs and other grids -->
        <ng-template #standardGrid>
          <div class="tiles" [style.--tile-min]="tileMinWidthPx">
            <article
              *ngFor="let item of items; trackBy: trackByItemId"
              class="tile"
              tabindex="0"
              role="button"
              (click)="onItemClick(item)"
              (keydown.enter)="onItemClick(item)"
              (keydown.space)="onItemKeydown($event, item)"
              [class.is-skeleton]="item.uiSkeleton"
            >
              <ng-container *ngIf="!item.uiSkeleton; else skeletonTile">
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
        </ng-template>
        </ng-container>
        <ng-template #emptyState>
          <div class="empty-hint">
            <div class="empty-hint__title">No items available yet</div>
            <div class="empty-hint__body">
              If this catalog just disappeared after Yjs reload, the webspace may still be rebuilding or the client may be using degraded fallback mode.
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
      }
      .tile {
        position: relative;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 6px;
        min-width: 0;
        padding: 14px 10px 12px;
        border-radius: 14px;
        border: 1px solid rgba(255, 255, 255, 0.08);
        background: rgba(255, 255, 255, 0.03);
        text-align: center;
        cursor: pointer;
        outline: none;
        transition: background 0.2s, transform 0.15s ease, border-color 0.2s ease;
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
      .tile.is-skeleton {
        cursor: default;
        pointer-events: none;
      }
      .icon-wrapper {
        font-size: 28px;
      }
      .icon-wrapper ion-icon {
        width: 40px;
        height: 40px;
      }
      .label {
        font-size: 13px;
        font-weight: 600;
        line-height: 1.25;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
      }
      .subtitle {
        font-size: 11px;
        opacity: 0.72;
        line-height: 1.3;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
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
        gap: 6px;
        justify-content: center;
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
        width: 40px;
        height: 40px;
        border-radius: 14px;
      }
      .project-name {
        font-weight: 600;
        font-size: 14px;
      }
      .project-type {
        font-size: 12px;
        opacity: 0.8;
      }
      .project-description {
        font-size: 12px;
        opacity: 0.8;
      }
      .project-updated {
        font-size: 11px;
        opacity: 0.7;
      }
      .empty-hint {
        padding: 18px 14px;
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
export class SchemaCollectionGridComponent implements OnInit, OnDestroy {
  @Input() widget!: WidgetConfig

  items$?: Observable<any[] | undefined>
  private kind: 'app' | 'widget' | undefined

  constructor(
    private data: PageDataService,
    private actions: PageActionService,
  ) {}

  ngOnInit(): void {
    this.items$ = this.data.load<any[]>(this.widget?.dataSource).pipe(
      map((items) => this.decorateItems(items)),
    )
    this.kind = this.inferKind()
  }

  ngOnDestroy(): void {
  }

  async onItemClick(item: any): Promise<void> {
    if (item?.uiSkeleton) return
    item = this.unwrapItem(item)
    const cfg = this.widget
    if (!cfg?.actions) return
    for (const act of cfg.actions) {
      await this.dispatchAction(act, item, cfg)
    }
  }

  private async dispatchAction(
    act: ActionConfig,
    event: any,
    widget: WidgetConfig
  ): Promise<void> {
    // В schema-модалках опираемся на декларативные действия (callHost и др.)
    await this.actions.handle(act, { event, widget })
  }

  isInstalled(item: any): boolean {
    return !!item?.installed
  }

  private inferKind(): 'app' | 'widget' | undefined {
    const actions = this.widget?.actions || []
    for (const act of actions) {
      const t = act.params?.['type']
      if (t === 'app' || t === 'widget') return t
    }
    const path = (this.widget?.dataSource as any)?.path as string | undefined
    if (path?.includes('/apps')) return 'app'
    if (path?.includes('/widgets')) return 'widget'
    return undefined
  }

  get isProjectSelector(): boolean {
    return this.widget?.id === 'project-select-list'
  }

  get tileMinWidthPx(): string {
    const raw = Number(this.widget?.inputs?.['tileMinWidth'] ?? 160)
    const value = !raw || raw < 120 ? 120 : Math.min(220, Math.floor(raw))
    return `${value}px`
  }

  onItemKeydown(event: Event, item: any): void {
    event.preventDefault()
    if (item?.uiSkeleton) return
    void this.onItemClick(item)
  }

  private itemSubtitle(item: any): string {
    return String(item?.subtitle || item?.description || item?.source || item?.origin || '').trim()
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
    item = this.unwrapItem(item)
    if (action === 'install') {
      const installType = item?.installType === 'app' ? 'app' : 'widget'
      await this.actions.toggleDesktopInstall(installType, String(item?.id || ''))
      this.applyLocalQuickActionResult(item, 'install')
      return
    }
    if (action === 'pin') {
      await this.actions.toggleDesktopPinnedWidget(item, !item?.pinned)
      this.applyLocalQuickActionResult(item, 'pin')
    }
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

  private applyLocalQuickActionResult(item: any, action: 'install' | 'pin'): void {
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

@Component({
  selector: 'ada-schema-modal',
  standalone: true,
  imports: [
    CommonModule,
    IonicModule,
    SchemaCollectionGridComponent,
    PageWidgetHostComponent,
  ],
  template: `
    <ion-header *ngIf="title">
      <ion-toolbar>
        <ion-title>{{ title }}</ion-title>
        <ion-buttons slot="end">
          <ion-button *ngIf="showFullscreenButton" (click)="toggleFullscreen()" aria-label="Fullscreen">
            <ion-icon [name]="isFullscreen ? 'contract-outline' : 'expand-outline'"></ion-icon>
          </ion-button>
          <ion-button (click)="dismiss()">Close</ion-button>
        </ion-buttons>
      </ion-toolbar>
    </ion-header>
    <ion-content>
      <div class="schema-modal">
          <ng-container *ngIf="schema">
          <ng-container *ngFor="let widget of schema.widgets">
            <!-- collection.grid-based catalog modals -->
            <ada-schema-collection-grid
              *ngIf="widget.type === 'collection.grid'"
              [widget]="widget"
            ></ada-schema-collection-grid>
            <!-- everything else: use the unified widget host -->
            <ada-page-widget-host
              *ngIf="widget.type !== 'collection.grid'"
              [widget]="widget"
            ></ada-page-widget-host>
          </ng-container>
        </ng-container>
      </div>
    </ion-content>
  `,
  styles: [
    `
      :host {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      ion-content {
        --padding-start: 12px;
        --padding-end: 12px;
        --padding-top: 12px;
        --padding-bottom: 12px;
      }
      .schema-modal {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
    `,
  ],
})
export class SchemaModalComponent {
  @Input() title?: string
  @Input() schema?: PageSchema

  private appliedInitialState = false
  showFullscreenButton = false
  isFullscreen = false

  constructor(
    private modalCtrl: ModalController,
    private pageState: PageStateService,
  ) {
    addIcons({
      'contract-outline': contractOutline,
      'expand-outline': expandOutline,
    })
  }

  ngOnInit(): void {
    this.showFullscreenButton = this.shouldShowFullscreenButton()
    this.applyInitialStateIfNeeded()
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.applyInitialStateIfNeeded()
  }

  dismiss(): void {
    this.modalCtrl.dismiss()
  }

  async toggleFullscreen(): Promise<void> {
    try {
      const top: any = await this.modalCtrl.getTop()
      if (!top) return
      this.isFullscreen = top.classList.toggle('ada-schema-modal-fullscreen')
    } catch {
      // best-effort
    }
  }

  private applyInitialStateIfNeeded(): void {
    if (this.appliedInitialState) return
    const init = this.schema?.initialState
    if (!init || typeof init !== 'object') return
    const snapshot = this.pageState.getSnapshot()
    const patch: Record<string, any> = {}
    for (const [k, v] of Object.entries(init)) {
      if (snapshot[k] === undefined) patch[k] = v
    }
    if (Object.keys(patch).length) {
      this.pageState.patch(patch)
    }
    this.appliedInitialState = true
  }

  private shouldShowFullscreenButton(): boolean {
    try {
      // On small screens Ionic modals are already fullscreen.
      return window.matchMedia('(min-width: 768px)').matches
    } catch {
      return false
    }
  }
}
