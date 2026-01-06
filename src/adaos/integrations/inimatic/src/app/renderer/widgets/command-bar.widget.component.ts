import { Component, Input, OnChanges, OnDestroy, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { PageActionService } from '../../runtime/page-action.service'
import { WidgetConfig, ActionConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { Subscription } from 'rxjs'
import { PageModalService } from '../../runtime/page-modal.service'
import { PageStateService } from '../../runtime/page-state.service'
import { addIcons } from 'ionicons'
import { folderOpenOutline } from 'ionicons/icons'

@Component({
  selector: 'ada-command-bar-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  providers: [PageModalService],
  template: `
    <ng-container [ngSwitch]="variant">
      <div *ngSwitchCase="'header'" class="headerbar">
        <div class="headerbar__row">
          <div class="headerbar__title">{{ widget.title || '' }}</div>
        </div>
        <div *ngIf="hasSelection" class="headerbar__selection">
          <div class="headerbar__value">{{ selectionValue }}</div>
          <ion-button
            *ngIf="primaryButton"
            class="headerbar__change"
            fill="clear"
            size="small"
            (click)="onClick(primaryButton)"
            [attr.aria-label]="primaryButton.label || primaryButton.id"
          >
            <ion-icon [name]="primaryButton.icon || 'folder-open-outline'"></ion-icon>
          </ion-button>
        </div>
        <ion-button
          *ngIf="primaryButton && !hasSelection"
          expand="block"
          (click)="onClick(primaryButton)"
        >
          <ion-icon slot="start" [name]="primaryButton.icon || 'folder-open-outline'"></ion-icon>
          {{ primaryButton.label }}
        </ion-button>
      </div>
      <ion-segment *ngSwitchDefault mode="md" [ngClass]="segmentClass">
        <ion-segment-button
          *ngFor="let btn of buttons"
          [value]="btn.id"
          [ngClass]="btn['kind'] === 'danger' ? 'segment-btn-danger' : ''"
          (click)="onClick(btn)"
        >
          <ion-label>{{ btn.label }}</ion-label>
        </ion-segment-button>
      </ion-segment>
    </ng-container>
  `,
  styles: [
    `
      ion-segment {
        width: 100%;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: none;
      }
      ion-segment::-webkit-scrollbar {
        display: none;
      }
      ion-segment-button {
        flex: 0 0 auto;
      }
      ion-segment.segment-small {
        transform: scale(0.9);
        transform-origin: left center;
      }
      ion-segment-button.segment-btn-danger {
        --color: var(--ion-color-danger);
        --indicator-color: var(--ion-color-danger);
      }
      .headerbar {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .headerbar__row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
      }
      .headerbar__selection {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
      }
      .headerbar__change {
        flex: 0 0 auto;
        --color: currentColor;
      }
      .headerbar__title {
        font-size: 13px;
        letter-spacing: 0.06em;
        opacity: 0.9;
        text-transform: uppercase;
      }
      .headerbar__value {
        flex: 1;
        font-size: 12px;
        opacity: 0.75;
        min-width: 0;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
    `,
  ],
})
export class CommandBarWidgetComponent implements OnInit, OnDestroy, OnChanges {
  @Input() widget!: WidgetConfig

  buttons: Array<{ id: string; label: string; [k: string]: any }> = []
  private dataSub?: Subscription
  private stateSub?: Subscription
  private rawButtons: Array<{ id: string; label: string; [k: string]: any }> = []
  segmentClass = ''
  variant: string = ''
  selectionValue = ''
  hasSelection = false
  primaryButton?: { id: string; label: string; icon?: string; [k: string]: any }

  constructor(
    private data: PageDataService,
    private actions: PageActionService,
    private modals: PageModalService,
    private state: PageStateService,
  ) {}

  ngOnInit(): void {
    addIcons({ folderOpenOutline })
    this.segmentClass = this.resolveSegmentClass()
    this.variant = this.resolveVariant()
    this.loadButtons()
    this.stateSub = this.state.selectAll().subscribe(() => {
      this.recomputeLabelsFromState()
    })
  }

  ngOnDestroy(): void {
    this.dataSub?.unsubscribe()
    this.stateSub?.unsubscribe()
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.segmentClass = this.resolveSegmentClass()
    this.variant = this.resolveVariant()
    this.loadButtons()
  }

  private loadButtons(): void {
    this.dataSub?.unsubscribe()
    if (this.widget?.dataSource) {
      const stream = this.data.load<any[]>(this.widget.dataSource)
      if (stream) {
        this.dataSub = stream.subscribe((items) => {
          this.rawButtons = Array.isArray(items)
            ? items.map((item, idx) => ({
                id: item.id || `btn-${idx}`,
                label: item.label || item.title || item.id || `Button ${idx + 1}`,
                ...item,
              }))
            : []
          this.recomputeLabelsFromState()
        })
      }
    } else {
      const raw = this.widget?.inputs?.['buttons']
      this.rawButtons = Array.isArray(raw) ? raw : []
      this.recomputeLabelsFromState()
    }
  }

  private recomputeLabelsFromState(): void {
    const snapshot = this.state.getSnapshot()
    this.buttons = this.rawButtons.map((btn, idx) => {
      const anyBtn: any = btn
      let label = btn.label || anyBtn['title'] || btn.id || `Button ${idx + 1}`
      if (typeof label === 'string' && label.startsWith('$state.')) {
        const key = label.slice('$state.'.length)
        const value = (snapshot as any)[key]
        if (value != null && value !== '') {
          label = String(value)
        } else {
          label = ''
        }
      }
      return { ...btn, label }
    })
    this.recomputeHeaderSelection(snapshot)
  }

  private recomputeHeaderSelection(snapshot: any): void {
    const key = (this.widget?.inputs as any)?.['selectedStateKey']
    const value = key ? snapshot?.[key] : undefined
    this.hasSelection = value != null && String(value).trim() !== ''
    this.selectionValue = this.hasSelection ? String(value) : ''
    const primaryId = (this.widget?.inputs as any)?.['primaryButtonId']
    const btn =
      (primaryId ? this.buttons.find((b) => b.id === primaryId) : undefined) ||
      this.buttons[0]
    this.primaryButton = btn
  }

  async onClick(btn: { id: string }): Promise<void> {
    const cfg = this.widget
    if (!cfg?.actions) return
    const event: any = { ...btn, ts: Date.now() }
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

  private resolveSegmentClass(): string {
    const size = this.widget?.inputs?.['size']
    if (size === 'small') return 'segment-small'
    return ''
  }

  private resolveVariant(): string {
    const v = (this.widget?.inputs as any)?.['variant']
    return typeof v === 'string' ? v : ''
  }
}
