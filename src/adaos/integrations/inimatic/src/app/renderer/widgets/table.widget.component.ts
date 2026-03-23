import { Component, Input, OnChanges, OnDestroy, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { Subscription } from 'rxjs'
import { PageDataService } from '../../runtime/page-data.service'
import { PageActionService } from '../../runtime/page-action.service'
import { PageModalService } from '../../runtime/page-modal.service'
import { ActionConfig, WidgetConfig } from '../../runtime/page-schema.model'

type TableColumn = {
  key: string
  label?: string
  width?: string
  mono?: boolean
}

type TableButton = {
  id: string
  label?: string
  icon?: string
  whenKey?: string
  whenEquals?: any
}

@Component({
  selector: 'ada-table-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  providers: [PageModalService],
  template: `
    <div class="table-widget">
      <h2 *ngIf="widget?.title">{{ widget.title }}</h2>

      <div class="table-wrap">
        <table class="t">
          <thead>
            <tr>
              <th *ngFor="let c of columns" [style.width]="c.width || null">
                {{ c.label || c.key }}
              </th>
              <th *ngIf="buttons.length" class="actions">Actions</th>
            </tr>
          </thead>
          <tbody>
            <tr *ngFor="let row of rows">
              <td *ngFor="let c of columns" [class.mono]="c.mono">
                {{ cellOf(row, c.key) }}
              </td>
              <td *ngIf="buttons.length" class="actions">
                <ion-buttons>
                  <ng-container *ngFor="let b of buttons">
                    <ion-button
                      *ngIf="shouldShowButton(b, row)"
                      fill="clear"
                      size="small"
                      (click)="onButtonClick($event, b, row)"
                    >
                      <ion-icon *ngIf="b.icon" [name]="b.icon" [slot]="b.label ? 'start' : 'icon-only'"></ion-icon>
                      <ng-container *ngIf="b.label">{{ b.label }}</ng-container>
                    </ion-button>
                  </ng-container>
                </ion-buttons>
              </td>
            </tr>
            <tr *ngIf="!rows.length && emptyText">
              <td [attr.colspan]="columns.length + (buttons.length ? 1 : 0)" class="empty">
                {{ emptyText }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  `,
  styles: [
    `
      .table-widget h2 {
        font-size: 14px;
        font-weight: 500;
        margin: 0 0 8px;
        text-transform: uppercase;
      }
      .table-wrap {
        width: 100%;
        overflow: auto;
        border: 1px solid rgba(0, 0, 0, 0.08);
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.6);
      }
      .t {
        width: 100%;
        border-collapse: collapse;
        min-width: 520px;
      }
      .t th,
      .t td {
        text-align: left;
        padding: 10px 12px;
        border-bottom: 1px solid rgba(0, 0, 0, 0.06);
        vertical-align: middle;
        font-size: 13px;
      }
      .t th {
        font-size: 12px;
        letter-spacing: 0.02em;
        text-transform: uppercase;
        opacity: 0.75;
        position: sticky;
        top: 0;
        background: rgba(250, 250, 250, 0.92);
        z-index: 1;
      }
      .t tr:last-child td {
        border-bottom: none;
      }
      .actions {
        white-space: nowrap;
      }
      .mono {
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New',
          monospace;
      }
      td.empty {
        opacity: 0.7;
        font-size: 12px;
        padding: 12px;
      }
    `,
  ],
})
export class TableWidgetComponent implements OnInit, OnChanges, OnDestroy {
  @Input() widget!: WidgetConfig

  columns: TableColumn[] = []
  buttons: TableButton[] = []
  emptyText = ''

  rows: any[] = []
  private dataSub?: Subscription

  constructor(
    private data: PageDataService,
    private actions: PageActionService,
    private modals: PageModalService,
  ) {}

  ngOnInit(): void {
    this.applyInputs()
    this.reload()
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.applyInputs()
    this.reload()
  }

  ngOnDestroy(): void {
    this.dataSub?.unsubscribe()
  }

  private applyInputs(): void {
    const inputs: any = this.widget?.inputs || {}
    this.emptyText = typeof inputs.emptyText === 'string' ? inputs.emptyText : ''
    this.columns = Array.isArray(inputs.columns)
      ? inputs.columns
          .filter((c: any) => c && typeof c === 'object' && c.key)
          .map((c: any) => ({
            key: String(c.key),
            label: typeof c.label === 'string' ? c.label : undefined,
            width: typeof c.width === 'string' ? c.width : undefined,
            mono: c.mono === true,
          }))
      : []
    if (!this.columns.length) {
      this.columns = [
        { key: 'name', label: 'Name' },
        { key: 'version', label: 'Version', mono: true, width: '160px' },
        { key: 'slot', label: 'Slot', mono: true, width: '90px' },
      ]
    }
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

  private reload(): void {
    this.dataSub?.unsubscribe()
    const stream = this.data.load<any>(this.widget?.dataSource)
    this.dataSub = stream.subscribe((payload) => {
      // API often returns { ok, items }.
      const items = Array.isArray((payload as any)?.items)
        ? (payload as any).items
        : Array.isArray(payload)
          ? payload
          : []
      this.rows = items
    })
  }

  cellOf(row: any, key: string): string {
    const v = this.getByPath(row, key)
    if (v == null) return ''
    if (typeof v === 'string') return v
    if (typeof v === 'number' || typeof v === 'boolean') return String(v)
    try {
      return JSON.stringify(v)
    } catch {
      return String(v)
    }
  }

  shouldShowButton(btn: TableButton, row: any): boolean {
    if (!btn.whenKey) return true
    const v = this.getByPath(row, btn.whenKey)
    if (btn.whenEquals === undefined) return Boolean(v)
    return v === btn.whenEquals
  }

  async onButtonClick(ev: Event, btn: { id: string }, row: any): Promise<void> {
    ev.preventDefault()
    ev.stopPropagation()
    const cfg = this.widget
    if (!cfg?.actions) return
    const event: any = { ...row, _button: btn.id }
    const eventId = `click:${btn.id}`
    for (const act of cfg.actions) {
      if (act.on === eventId || act.on === 'click') {
        await this.dispatchAction(act, event, cfg)
      }
    }
    // Common expected behavior: after an imperative action, refresh the table.
    this.reload()
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
      return this.getByPath(event, value.slice('$event.'.length))
    }
    return value
  }

  private getByPath(obj: any, path: string): any {
    if (!obj || !path) return undefined
    return path.split('.').reduce((acc, k) => (acc != null ? (acc as any)[k] : undefined), obj)
  }
}

