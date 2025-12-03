import { Component, Input, OnChanges, OnDestroy, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { PageActionService } from '../../runtime/page-action.service'
import { WidgetConfig, ActionConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { Subscription } from 'rxjs'
import { PageModalService } from '../../runtime/page-modal.service'

@Component({
  selector: 'ada-command-bar-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  providers: [PageModalService],
  template: `
    <ion-segment mode="md" [ngClass]="segmentClass">
      <ion-segment-button
        *ngFor="let btn of buttons"
        [value]="btn.id"
        (click)="onClick(btn)"
      >
        <ion-label>{{ btn.label }}</ion-label>
      </ion-segment-button>
    </ion-segment>
  `,
  styles: [
    `
      ion-segment.segment-small {
        transform: scale(0.9);
        transform-origin: left center;
      }
    `,
  ],
})
export class CommandBarWidgetComponent implements OnInit, OnDestroy, OnChanges {
  @Input() widget!: WidgetConfig

  buttons: Array<{ id: string; label: string; [k: string]: any }> = []
  private dataSub?: Subscription
  segmentClass = ''

  constructor(
    private data: PageDataService,
    private actions: PageActionService,
    private modals: PageModalService,
  ) {}

  ngOnInit(): void {
    this.segmentClass = this.resolveSegmentClass()
    this.loadButtons()
  }

  ngOnDestroy(): void {
    this.dataSub?.unsubscribe()
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.segmentClass = this.resolveSegmentClass()
    this.loadButtons()
  }

  private loadButtons(): void {
    this.dataSub?.unsubscribe()
    if (this.widget?.dataSource) {
      const stream = this.data.load<any[]>(this.widget.dataSource)
      if (stream) {
        this.dataSub = stream.subscribe((items) => {
          this.buttons = Array.isArray(items)
            ? items.map((item, idx) => ({
                id: item.id || `btn-${idx}`,
                label: item.label || item.title || item.id || `Button ${idx + 1}`,
                ...item,
              }))
            : []
        })
      }
    } else {
      const raw = this.widget?.inputs?.['buttons']
      this.buttons = Array.isArray(raw) ? raw : []
    }
  }

  async onClick(btn: { id: string }): Promise<void> {
    const cfg = this.widget
    if (!cfg?.actions) return
    const eventId = `click:${btn.id}`
    for (const act of cfg.actions) {
      if (act.on === eventId || act.on === 'click') {
        await this.dispatchAction(act, btn, cfg)
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
}
