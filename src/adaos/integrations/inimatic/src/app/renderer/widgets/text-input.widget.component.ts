import { Component, Input, OnChanges, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { FormsModule } from '@angular/forms'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageActionService } from '../../runtime/page-action.service'

@Component({
  selector: 'ada-text-input-widget',
  standalone: true,
  imports: [CommonModule, IonicModule, FormsModule],
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

  constructor(private actions: PageActionService) {}

  ngOnInit(): void {
    this.value = this.widget?.inputs?.['initialValue'] || ''
  }

  ngOnChanges(_changes: SimpleChanges): void {
    // keep current value; inputs are mostly static
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
}

