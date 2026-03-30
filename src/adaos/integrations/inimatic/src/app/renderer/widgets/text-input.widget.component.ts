import { Component, Input, OnChanges, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicStandaloneImports } from '../../shared/ionic-standalone'
import { FormsModule } from '@angular/forms'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageActionService } from '../../runtime/page-action.service'
import { PageDataService } from '../../runtime/page-data.service'
import { Subscription } from 'rxjs'

@Component({
  selector: 'ada-text-input-widget',
  standalone: true,
  imports: [CommonModule, IonicStandaloneImports, FormsModule],
  template: `
    <ion-item lines="full">
      <ion-label position="stacked">
        {{ widget.title || widget.inputs?.['label'] || 'Name' }}
      </ion-label>
      <ion-input
        [placeholder]="widget.inputs?.['placeholder'] || ''"
        [(ngModel)]="value"
        (ionBlur)="onChange()"
      ></ion-input>
    </ion-item>
  `,
})
export class TextInputWidgetComponent implements OnInit, OnChanges {
  @Input() widget!: WidgetConfig

  value = ''
  private dataSub?: Subscription

  constructor(
    private actions: PageActionService,
    private data: PageDataService
  ) {}

  ngOnInit(): void {
    this.loadInitial()
  }

  ngOnChanges(_changes: SimpleChanges): void {
    // keep current value; inputs are mostly static for now
  }

  async onChange(): Promise<void> {
    if (!this.widget?.actions) return
    const event = { value: this.value }
    for (const act of this.widget.actions) {
      if (act.on === 'change') {
        await this.actions.handle(act, { event, widget: this.widget })
      }
    }
  }

  private loadInitial(): void {
    this.dataSub?.unsubscribe()
    const ds = this.widget?.dataSource
    if (ds) {
      const bindField: string =
        (this.widget.inputs && this.widget.inputs['bindField']) || 'value'
      this.dataSub = this.data.load<any>(ds).subscribe({
        next: (value) => {
          try {
            const next =
              value && typeof value === 'object' ? (value as any)[bindField] : undefined
            this.value = typeof next === 'string' ? next : this.widget?.inputs?.['initialValue'] || ''
          } catch {
            this.value = this.widget?.inputs?.['initialValue'] || ''
          }
        },
        error: () => {
          this.value = this.widget?.inputs?.['initialValue'] || ''
        },
      })
    } else {
      this.value = this.widget?.inputs?.['initialValue'] || ''
    }
  }
}
