import { Component, Input, OnChanges, OnDestroy, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { Observable, Subscription } from 'rxjs'
import { PageDataService } from '../../runtime/page-data.service'
import { PageActionService } from '../../runtime/page-action.service'
import { PageModalService } from '../../runtime/page-modal.service'
import { ActionConfig, WidgetConfig } from '../../runtime/page-schema.model'

type ListButton = {
  id: string
  label?: string
  icon?: string
  whenKey?: string
  whenEquals?: any
}

type ListGroup = {
  key: string
  title: string
  subtitle: string
  items: any[]
  subgroups?: ListSubGroup[]
}

type ListSubGroup = {
  key: string
  title: string
  subtitle: string
  items: any[]
}

@Component({
  selector: 'ada-list-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  providers: [PageModalService],
  template: `
    <div class="list-widget">
      <h2 *ngIf="widget?.title">{{ widget.title }}</h2>

      <ng-container *ngIf="groupByKey; else flatList">
        <ion-accordion-group>
          <ion-accordion *ngFor="let g of grouped">
            <ion-item slot="header" [detail]="false">
              <ion-label>
                <div class="title">{{ g.title }}</div>
                <div class="subtitle" *ngIf="g.subtitle">{{ g.subtitle }}</div>
              </ion-label>
            </ion-item>
            <div slot="content">
              <ng-container *ngIf="g.subgroups?.length; else groupedOneLevel">
                <ion-accordion-group>
                  <ion-accordion *ngFor="let sg of g.subgroups">
                    <ion-item slot="header" [detail]="false">
                      <ion-label>
                        <div class="title">{{ sg.title }}</div>
                        <div class="subtitle" *ngIf="sg.subtitle">{{ sg.subtitle }}</div>
                      </ion-label>
                    </ion-item>
                    <div slot="content">
                      <ng-container *ngTemplateOutlet="itemsTemplate; context: { items: sg.items }"></ng-container>
                    </div>
                  </ion-accordion>
                </ion-accordion-group>
              </ng-container>

              <ng-template #groupedOneLevel>
                <ng-container *ngTemplateOutlet="itemsTemplate; context: { items: g.items }"></ng-container>
              </ng-template>
            </div>
          </ion-accordion>
        </ion-accordion-group>
      </ng-container>

      <ng-template #itemsTemplate let-items="items">
        <ng-container *ngIf="itemAccordion; else groupedFlat">
          <ion-accordion-group>
            <ion-accordion *ngFor="let item of items">
              <ion-item slot="header" [detail]="false" (click)="onSelect(item)">
                <ion-icon *ngIf="iconOf(item)" [name]="iconOf(item)" slot="start"></ion-icon>
                <ion-label>
                  <div class="title">{{ titleOf(item) }}</div>
                  <div class="subtitle" *ngIf="subtitleOf(item)">{{ subtitleOf(item) }}</div>
                </ion-label>

                <ion-buttons slot="end" *ngIf="buttons.length">
                  <ng-container *ngFor="let b of buttons">
                    <ion-button
                      *ngIf="shouldShowButton(b, item)"
                      fill="clear"
                      size="small"
                      (click)="onButtonClick($event, b, item)"
                    >
                      <ion-icon *ngIf="b.icon" [name]="b.icon" slot="icon-only"></ion-icon>
                      <ng-container *ngIf="!b.icon">{{ b.label }}</ng-container>
                    </ion-button>
                  </ng-container>
                </ion-buttons>
              </ion-item>
              <div slot="content">
                <ng-container *ngIf="detailsTextOf(item) as details; else noDetailsGrouped">
                  <pre class="details">{{ details }}</pre>
                </ng-container>
                <ng-template #noDetailsGrouped>
                  <div class="empty-details">No details</div>
                </ng-template>
              </div>
            </ion-accordion>
          </ion-accordion-group>
        </ng-container>

        <ng-template #groupedFlat>
          <ion-list [inset]="inset">
            <ng-container *ngFor="let item of items">
              <ion-item button (click)="onSelect(item)" [detail]="false">
                <ion-icon *ngIf="iconOf(item)" [name]="iconOf(item)" slot="start"></ion-icon>
                <ion-label>
                  <div class="title">{{ titleOf(item) }}</div>
                  <div class="subtitle" *ngIf="subtitleOf(item)">{{ subtitleOf(item) }}</div>
                </ion-label>

                <ion-buttons slot="end" *ngIf="buttons.length">
                  <ng-container *ngFor="let b of buttons">
                    <ion-button
                      *ngIf="shouldShowButton(b, item)"
                      fill="clear"
                      size="small"
                      (click)="onButtonClick($event, b, item)"
                    >
                      <ion-icon *ngIf="b.icon" [name]="b.icon" slot="icon-only"></ion-icon>
                      <ng-container *ngIf="!b.icon">{{ b.label }}</ng-container>
                    </ion-button>
                  </ng-container>
                </ion-buttons>
              </ion-item>
              <ng-container *ngIf="showDetails">
                <ng-container *ngIf="detailsTextOf(item) as details; else noDetailsGroupedFlat">
                  <pre class="details">{{ details }}</pre>
                </ng-container>
                <ng-template #noDetailsGroupedFlat>
                  <div class="empty-details">No details</div>
                </ng-template>
              </ng-container>
            </ng-container>

            <ion-item *ngIf="!items.length && emptyText">
              <ion-label>{{ emptyText }}</ion-label>
            </ion-item>
          </ion-list>
        </ng-template>
      </ng-template>

      <ng-template #flatList>
        <ion-list *ngIf="items$ | async as items" [inset]="inset">
          <ng-container *ngIf="itemAccordion; else flatRows">
            <ion-accordion-group>
              <ion-accordion *ngFor="let item of items">
                <ion-item slot="header" [detail]="false" (click)="onSelect(item)">
                  <ion-icon *ngIf="iconOf(item)" [name]="iconOf(item)" slot="start"></ion-icon>
                  <ion-label>
                    <div class="title">{{ titleOf(item) }}</div>
                    <div class="subtitle" *ngIf="subtitleOf(item)">{{ subtitleOf(item) }}</div>
                  </ion-label>

                  <ion-buttons slot="end" *ngIf="buttons.length">
                    <ng-container *ngFor="let b of buttons">
                      <ion-button
                        *ngIf="shouldShowButton(b, item)"
                        fill="clear"
                        size="small"
                        (click)="onButtonClick($event, b, item)"
                      >
                        <ion-icon *ngIf="b.icon" [name]="b.icon" slot="icon-only"></ion-icon>
                        <ng-container *ngIf="!b.icon">{{ b.label }}</ng-container>
                      </ion-button>
                    </ng-container>
                  </ion-buttons>
                </ion-item>
                <div slot="content">
                  <ng-container *ngIf="detailsTextOf(item) as details; else noDetailsFlat">
                    <pre class="details">{{ details }}</pre>
                  </ng-container>
                  <ng-template #noDetailsFlat>
                    <div class="empty-details">No details</div>
                  </ng-template>
                </div>
              </ion-accordion>
            </ion-accordion-group>
          </ng-container>

          <ng-template #flatRows>
            <ng-container *ngFor="let item of items">
              <ion-item button (click)="onSelect(item)" [detail]="false">
                <ion-icon *ngIf="iconOf(item)" [name]="iconOf(item)" slot="start"></ion-icon>
                <ion-label>
                  <div class="title">{{ titleOf(item) }}</div>
                  <div class="subtitle" *ngIf="subtitleOf(item)">{{ subtitleOf(item) }}</div>
                </ion-label>

                <ion-buttons slot="end" *ngIf="buttons.length">
                  <ng-container *ngFor="let b of buttons">
                    <ion-button
                      *ngIf="shouldShowButton(b, item)"
                      fill="clear"
                      size="small"
                      (click)="onButtonClick($event, b, item)"
                    >
                      <ion-icon *ngIf="b.icon" [name]="b.icon" slot="icon-only"></ion-icon>
                      <ng-container *ngIf="!b.icon">{{ b.label }}</ng-container>
                    </ion-button>
                  </ng-container>
                </ion-buttons>
              </ion-item>
              <ng-container *ngIf="showDetails">
                <ng-container *ngIf="detailsTextOf(item) as details; else noDetailsFlatRows">
                  <pre class="details">{{ details }}</pre>
                </ng-container>
                <ng-template #noDetailsFlatRows>
                  <div class="empty-details">No details</div>
                </ng-template>
              </ng-container>
            </ng-container>
          </ng-template>

          <ion-item *ngIf="!items.length && emptyText">
            <ion-label>{{ emptyText }}</ion-label>
          </ion-item>
        </ion-list>
      </ng-template>
    </div>
  `,
  styles: [
    `
      .list-widget h2 {
        font-size: 14px;
        font-weight: 500;
        margin: 0 0 8px;
        text-transform: uppercase;
      }
      .title {
        font-weight: 500;
      }
      .subtitle {
        font-size: 12px;
        opacity: 0.75;
        margin-top: 2px;
      }
      .details {
        margin: 0;
        padding: 8px 12px;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New',
          monospace;
        font-size: 12px;
        white-space: pre-wrap;
        word-break: break-word;
        opacity: 0.9;
      }
      .empty-details {
        padding: 8px 12px;
        font-size: 12px;
        opacity: 0.7;
      }
    `,
  ],
})
export class ListWidgetComponent implements OnInit, OnChanges, OnDestroy {
  @Input() widget!: WidgetConfig

  items$?: Observable<any[] | undefined>
  private dataSub?: Subscription
  private latestItems: any[] = []
  grouped: ListGroup[] = []

  inset = false
  emptyText = ''

  titleKey = 'title'
  subtitleKey = 'subtitle'
  iconKey = 'icon'

  groupByKey = ''
  groupTitleKey = ''
  groupSubtitleKey = ''

  subGroupByKey = ''
  subGroupTitleKey = ''
  subGroupSubtitleKey = ''

  itemAccordion = false
  showDetails = false
  detailsPath = ''
  detailsModal = false
  detailsModalTitleKey = ''

  buttons: ListButton[] = []

  constructor(
    private data: PageDataService,
    private actions: PageActionService,
    private modals: PageModalService,
  ) {}

  ngOnInit(): void {
    this.applyInputs()
    this.updateItemsStream()
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.applyInputs()
    this.updateItemsStream()
  }

  ngOnDestroy(): void {
    this.dataSub?.unsubscribe()
  }

  private applyInputs(): void {
    const inputs: any = this.widget?.inputs || {}
    this.inset = inputs.inset === true
    this.emptyText = typeof inputs.emptyText === 'string' ? inputs.emptyText : ''
    this.titleKey = typeof inputs.titleKey === 'string' ? inputs.titleKey : 'title'
    this.subtitleKey = typeof inputs.subtitleKey === 'string' ? inputs.subtitleKey : 'subtitle'
    this.iconKey = typeof inputs.iconKey === 'string' ? inputs.iconKey : 'icon'
    this.groupByKey = typeof inputs.groupBy === 'string' ? inputs.groupBy : ''
    this.groupTitleKey = typeof inputs.groupTitleKey === 'string' ? inputs.groupTitleKey : ''
    this.groupSubtitleKey = typeof inputs.groupSubtitleKey === 'string' ? inputs.groupSubtitleKey : ''
    this.subGroupByKey = typeof inputs.subGroupBy === 'string' ? inputs.subGroupBy : ''
    this.subGroupTitleKey = typeof inputs.subGroupTitleKey === 'string' ? inputs.subGroupTitleKey : ''
    this.subGroupSubtitleKey =
      typeof inputs.subGroupSubtitleKey === 'string' ? inputs.subGroupSubtitleKey : ''
    this.itemAccordion = inputs.itemAccordion === true
    this.showDetails = inputs.showDetails === true
    this.detailsPath = typeof inputs.detailsPath === 'string' ? inputs.detailsPath : ''
    this.detailsModal = inputs.detailsModal === true
    this.detailsModalTitleKey =
      typeof inputs.detailsModalTitleKey === 'string' ? inputs.detailsModalTitleKey : ''
    this.buttons = Array.isArray(inputs.buttons)
      ? inputs.buttons
          .filter((b: any) => b && typeof b === 'object' && (b.id || b.label))
          .map((b: any) => ({
            id: String(b.id || ''),
            label: typeof b.label === 'string' ? b.label : undefined,
            icon: typeof b.icon === 'string' ? b.icon : undefined,
            whenKey: typeof b.whenKey === 'string' ? b.whenKey : undefined,
            whenEquals: b.whenEquals,
          }))
          .filter((b: any) => b.id)
      : []
  }

  private updateItemsStream(): void {
    this.dataSub?.unsubscribe()
    this.items$ = this.data.load<any[]>(this.widget?.dataSource)
    const stream = this.items$
    if (!stream) return
    this.dataSub = stream.subscribe((items) => {
      this.latestItems = Array.isArray(items) ? items : []
      this.recomputeGroups()
    })
  }

  titleOf(item: any): string {
    const v =
      this.getByPath(item, this.titleKey) ??
      item?.title ??
      item?.label ??
      item?.id ??
      ''
    return String(v || '')
  }

  subtitleOf(item: any): string {
    const v = this.getByPath(item, this.subtitleKey)
    return typeof v === 'string' ? v : ''
  }

  iconOf(item: any): string {
    const v = this.getByPath(item, this.iconKey)
    return typeof v === 'string' ? v : ''
  }

  detailsTextOf(item: any): string {
    const raw = this.detailsPath ? this.getByPath(item, this.detailsPath) : item
    if (raw == null) return ''
    if (typeof raw === 'string') return raw
    try {
      return JSON.stringify(raw, null, 2)
    } catch {
      return String(raw)
    }
  }

  shouldShowButton(btn: ListButton, item: any): boolean {
    if (!btn.whenKey) return true
    const v = this.getByPath(item, btn.whenKey)
    return v === btn.whenEquals
  }

  async onSelect(item: any): Promise<void> {
    const cfg = this.widget
    const actions = Array.isArray(cfg?.actions) ? cfg.actions : []
    const hasSelectActions = actions.some((a) => a?.on === 'select')

    for (const act of actions) {
      if (act.on === 'select') {
        await this.dispatchAction(act, item, cfg)
      }
    }

    if (this.detailsModal && !hasSelectActions) {
      await this.openDetailsModal(item)
    }
  }

  async onButtonClick(ev: Event, btn: { id: string }, item: any): Promise<void> {
    ev.preventDefault()
    ev.stopPropagation()
    const cfg = this.widget
    if (!cfg?.actions) return
    const event: any = { ...item, _button: btn.id }
    const eventId = `click:${btn.id}`
    for (const act of cfg.actions) {
      if (act.on === eventId || act.on === 'click') {
        await this.dispatchAction(act, event, cfg)
      }
    }
  }

  private async dispatchAction(act: ActionConfig, event: any, widget: WidgetConfig): Promise<void> {
    if (act.type === 'openModal') {
      const modalId = this.resolveValue(act.params?.['modalId'], event)
      await this.modals.openModalById(modalId)
      return
    }
    await this.actions.handle(act, { event, widget })
  }

  private resolveValue(value: any, event: any): any {
    if (typeof value !== 'string') return value
    if (value.startsWith('$event.')) {
      const path = value.slice('$event.'.length)
      return path.split('.').reduce((acc, key) => (acc != null ? acc[key] : undefined), event)
    }
    return value
  }

  private getByPath(obj: any, path: string): any {
    if (!path) return undefined
    if (!path.includes('.')) return obj?.[path]
    return path.split('.').reduce((acc, key) => (acc != null ? acc[key] : undefined), obj)
  }

  private async openDetailsModal(item: any): Promise<void> {
    const titleRaw = this.detailsModalTitleKey ? this.getByPath(item, this.detailsModalTitleKey) : undefined
    const title = typeof titleRaw === 'string' ? titleRaw : titleRaw != null ? String(titleRaw) : 'Details'
    const value = this.detailsPath ? this.getByPath(item, this.detailsPath) : item

    await this.modals.openTransientSchemaModal({
      title,
      schema: {
        id: 'list_item_details',
        layout: { type: 'single', areas: [{ id: 'main', role: 'main' }] },
        widgets: [
          {
            id: 'details',
            type: 'item.details',
            area: 'main',
            dataSource: { kind: 'static', value },
          },
        ],
      } as any,
    })
  }

  private recomputeGroups(): void {
    if (!this.groupByKey) {
      this.grouped = []
      return
    }

    const groups = new Map<string, ListGroup>()
    const subgroups = new Map<string, Map<string, ListSubGroup>>()

    for (const item of this.latestItems) {
      const rawKey = this.getByPath(item, this.groupByKey)
      const key = rawKey == null ? '' : String(rawKey)

      if (!groups.has(key)) {
        const titleRaw = this.groupTitleKey ? this.getByPath(item, this.groupTitleKey) : key
        const subtitleRaw = this.groupSubtitleKey ? this.getByPath(item, this.groupSubtitleKey) : ''
        groups.set(key, {
          key,
          title: typeof titleRaw === 'string' ? titleRaw : String(titleRaw ?? key),
          subtitle: typeof subtitleRaw === 'string' ? subtitleRaw : String(subtitleRaw ?? ''),
          items: [],
        })
      }

      if (this.subGroupByKey) {
        const rawSubKey = this.getByPath(item, this.subGroupByKey)
        const subKey = rawSubKey == null ? '' : String(rawSubKey)
        if (!subgroups.has(key)) subgroups.set(key, new Map())
        const m = subgroups.get(key)!
        if (!m.has(subKey)) {
          const titleRaw = this.subGroupTitleKey ? this.getByPath(item, this.subGroupTitleKey) : subKey
          const subtitleRaw = this.subGroupSubtitleKey ? this.getByPath(item, this.subGroupSubtitleKey) : ''
          m.set(subKey, {
            key: subKey,
            title: typeof titleRaw === 'string' ? titleRaw : String(titleRaw ?? subKey),
            subtitle: typeof subtitleRaw === 'string' ? subtitleRaw : String(subtitleRaw ?? ''),
            items: [],
          })
        }
        m.get(subKey)!.items.push(item)
      } else {
        groups.get(key)!.items.push(item)
      }
    }

    const out = Array.from(groups.values())
    if (this.subGroupByKey) {
      for (const g of out) {
        const m = subgroups.get(g.key)
        g.subgroups = m ? Array.from(m.values()) : []
        g.items = []
      }
    }
    this.grouped = out
  }
}

