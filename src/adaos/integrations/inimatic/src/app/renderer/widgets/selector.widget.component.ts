import { Component, Input, OnChanges, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { Observable } from 'rxjs'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { PageActionService } from '../../runtime/page-action.service'

@Component({
  selector: 'ada-selector-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `
    <ion-item lines="full">
      <ion-label position="stacked">{{ widget.title || widget.inputs?.['label'] || 'Select' }}</ion-label>
      <ion-select
        [value]="currentValue"
        (ionChange)="onChange($event.detail.value)"
      >
        <ion-select-option
          *ngFor="let option of options"
          [value]="optionValue(option)"
        >
          {{ optionLabel(option) }}
        </ion-select-option>
      </ion-select>
    </ion-item>
  `,
})
export class SelectorWidgetComponent implements OnInit, OnChanges {
  @Input() widget!: WidgetConfig

  currentValue?: string
  options: any[] = []

  private data$?: Observable<any>

  constructor(
    private data: PageDataService,
    private actions: PageActionService
  ) {}

  ngOnInit(): void {
    this.options = this.normalizeOptions(this.widget?.inputs?.['options'])
    this.setupStream()
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['widget']) {
      this.options = this.normalizeOptions(this.widget?.inputs?.['options'])
      this.setupStream()
    }
  }

  private setupStream(): void {
    if (!this.widget?.dataSource) return
    const stream = this.data.load<any>(this.widget.dataSource)
    this.data$ = stream
    stream.subscribe((value) => {
      if (Array.isArray(value)) {
        this.options = this.normalizeOptions(value)
        return
      }
      const city = (value && (value.city || value.label)) as string | undefined
      this.currentValue = city
    })
  }

  async onChange(value: string): Promise<void> {
    this.currentValue = value
    if (!this.widget?.actions) return
    for (const act of this.widget.actions) {
      if (act.on === 'change') {
        await this.actions.handle(act, { event: { value } })
      }
    }
  }

  private normalizeOptions(raw: any): any[] {
    if (!raw) return []
    if (Array.isArray(raw)) {
      return raw
    }
    return []
  }

  optionLabel(option: any): string {
    if (option && typeof option === 'object') {
      return option.label || option.id || String(option)
    }
    return String(option)
  }

  optionValue(option: any): any {
    if (option && typeof option === 'object') {
      return option.id ?? option.value ?? option.label ?? option
    }
    return option
  }
}
