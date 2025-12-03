// src\adaos\integrations\inimatic\src\app\renderer\modals\schema-modal.component.ts
import { Component, Input, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule, ModalController } from '@ionic/angular'
import { Observable } from 'rxjs'
import { PageSchema, WidgetConfig, ActionConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { PageActionService } from '../../runtime/page-action.service'
import { YDocService } from '../../y/ydoc.service'
import { observeDeep } from '../../y/y-helpers'
import { MetricTileWidgetComponent } from '../widgets/metric-tile.widget.component'
import { SelectorWidgetComponent } from '../widgets/selector.widget.component'
import { TextInputWidgetComponent } from '../widgets/text-input.widget.component'
import { CommandBarWidgetComponent } from '../widgets/command-bar.widget.component'

@Component({
  selector: 'ada-schema-collection-grid',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `
    <div class="grid-section" *ngIf="widget">
      <h2 *ngIf="widget.title">{{ widget.title }}</h2>
      <ion-grid *ngIf="items$ | async as items">
        <ion-row>
          <ion-col
            *ngFor="let item of items"
            size="12"
            class="collection-grid-item"
          >
            <ion-item lines="none">
              <ion-toggle
                slot="start"
                [checked]="isInstalled(item)"
                (ionChange)="onToggleChange($event, item)"
              ></ion-toggle>
              <div class="icon-wrapper" *ngIf="item.icon">
                <ion-icon [name]="item.icon"></ion-icon>
              </div>
              <ion-label>
                <div class="label">{{ item.title || item.id }}</div>
              </ion-label>
            </ion-item>
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
        padding: 4px 0;
      }
      .icon-wrapper {
        font-size: 24px;
      }
      .icon-wrapper ion-icon {
        width: 32px;
        height: 32px;
      }
      .label {
        font-size: 14px;
      }
    `,
  ],
})
export class SchemaCollectionGridComponent implements OnInit, OnDestroy {
  @Input() widget!: WidgetConfig

  items$?: Observable<any[] | undefined>
  private installed = new Set<string>()
  private installedUnsub?: () => void
  private kind: 'app' | 'widget' | undefined

  constructor(
    private data: PageDataService,
    private actions: PageActionService,
    private ydoc: YDocService
  ) {}

  ngOnInit(): void {
    this.items$ = this.data.load<any[]>(this.widget?.dataSource)
    this.kind = this.inferKind()
    this.observeInstalled()
  }

  ngOnDestroy(): void {
    this.installedUnsub?.()
  }

  async onItemClick(item: any): Promise<void> {
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
    const id = item?.id
    if (!id) return false
    return this.installed.has(String(id))
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

  private observeInstalled(): void {
    this.installedUnsub?.()
    if (!this.kind) return
    const path =
      this.kind === 'app'
        ? 'data/installed/apps'
        : 'data/installed/widgets'
    const node: any = this.ydoc.getPath(path)
    const recompute = () => {
      try {
        const raw = this.ydoc.toJSON(node)
        const list: any[] = Array.isArray(raw) ? raw : []
        this.installed = new Set(list.map((v) => String(v)))
      } catch {
        this.installed = new Set()
      }
    }
    this.installedUnsub = observeDeep(node, recompute)
    recompute()
  }

  async onToggleChange(_ev: CustomEvent, item: any): Promise<void> {
    const cfg = this.widget
    if (!cfg?.actions) return
    for (const act of cfg.actions) {
      await this.dispatchAction(act, item, cfg)
    }
  }
}

@Component({
  selector: 'ada-schema-modal',
  standalone: true,
  imports: [
    CommonModule,
    IonicModule,
    SchemaCollectionGridComponent,
    MetricTileWidgetComponent,
    SelectorWidgetComponent,
    TextInputWidgetComponent,
    CommandBarWidgetComponent,
  ],
  template: `
    <ion-header *ngIf="title">
      <ion-toolbar>
        <ion-title>{{ title }}</ion-title>
        <ion-buttons slot="end">
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
            <!-- simple metric-tile based modals (e.g. weather summary) -->
            <ada-metric-tile-widget
              *ngIf="widget.type === 'visual.metricTile'"
              [widget]="widget"
            ></ada-metric-tile-widget>
            <!-- selector-based widgets, e.g. city picker -->
            <ada-selector-widget
              *ngIf="widget.type === 'input.selector'"
              [widget]="widget"
            ></ada-selector-widget>
            <!-- text input widgets (e.g. project name) -->
            <ada-text-input-widget
              *ngIf="widget.type === 'input.text'"
              [widget]="widget"
            ></ada-text-input-widget>
            <!-- command bar actions (e.g. Create button) -->
            <ada-command-bar-widget
              *ngIf="widget.type === 'input.commandBar'"
              [widget]="widget"
            ></ada-command-bar-widget>
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

  constructor(private modalCtrl: ModalController) {}

  dismiss(): void {
    this.modalCtrl.dismiss()
  }
}
