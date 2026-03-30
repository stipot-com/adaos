import { Component, Input, OnChanges, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicStandaloneImports } from '../../shared/ionic-standalone'
import { FormsModule } from '@angular/forms'
import { ActionConfig, WidgetConfig } from '../../runtime/page-schema.model'
import { PageActionService } from '../../runtime/page-action.service'
import { PageStateService } from '../../runtime/page-state.service'
import { PageModalService } from '../../runtime/page-modal.service'

type Field =
  | { id: string; type: 'text'; label?: string; placeholder?: string; stateKey?: string }
  | { id: string; type: 'number'; label?: string; placeholder?: string; stateKey?: string }
  | { id: string; type: 'toggle'; label?: string; stateKey?: string }
  | { id: string; type: 'select'; label?: string; options: string[]; stateKey?: string }

@Component({
  selector: 'ada-form-widget',
  standalone: true,
  imports: [CommonModule, IonicStandaloneImports, FormsModule],
  providers: [PageModalService],
  template: `
    <div class="form-widget">
      <h2 *ngIf="widget?.title">{{ widget.title }}</h2>

      <div class="fields">
        <ng-container *ngFor="let f of fields">
          <ion-item *ngIf="f.type === 'text'">
            <ion-label position="stacked">{{ f.label || f.id }}</ion-label>
            <ion-input
              [(ngModel)]="values[f.id]"
              [placeholder]="f.placeholder || ''"
            ></ion-input>
          </ion-item>

          <ion-item *ngIf="f.type === 'number'">
            <ion-label position="stacked">{{ f.label || f.id }}</ion-label>
            <ion-input
              type="number"
              [(ngModel)]="values[f.id]"
              [placeholder]="f.placeholder || ''"
            ></ion-input>
          </ion-item>

          <ion-item *ngIf="f.type === 'toggle'">
            <ion-label>{{ f.label || f.id }}</ion-label>
            <ion-toggle slot="end" [(ngModel)]="values[f.id]"></ion-toggle>
          </ion-item>

          <ion-item *ngIf="f.type === 'select'">
            <ion-label position="stacked">{{ f.label || f.id }}</ion-label>
            <ion-select [(ngModel)]="values[f.id]">
              <ion-select-option *ngFor="let opt of f.options" [value]="opt">{{ opt }}</ion-select-option>
            </ion-select>
          </ion-item>
        </ng-container>
      </div>

      <div class="actions" *ngIf="hasSubmit">
        <ion-button expand="block" (click)="onSubmit()">{{ submitLabel }}</ion-button>
      </div>
    </div>
  `,
  styles: [
    `
      .form-widget h2 {
        font-size: 14px;
        font-weight: 500;
        margin: 0 0 8px;
        text-transform: uppercase;
      }
      .fields {
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .actions {
        margin-top: 12px;
      }
    `,
  ],
})
export class FormWidgetComponent implements OnInit, OnChanges {
  @Input() widget!: WidgetConfig

  fields: Field[] = []
  values: Record<string, any> = {}

  hasSubmit = false
  submitLabel = 'Submit'

  constructor(
    private actions: PageActionService,
    private state: PageStateService,
    private modals: PageModalService,
  ) {}

  ngOnInit(): void {
    this.applyInputs()
    this.hydrateFromState()
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.applyInputs()
    this.hydrateFromState()
  }

  private applyInputs(): void {
    const inputs: any = this.widget?.inputs || {}
    const raw = inputs.fields
    this.fields = Array.isArray(raw)
      ? (raw
          .filter((f: any) => f && typeof f === 'object' && f.id && f.type)
          .map((f: any) => ({
            id: String(f.id),
            type: f.type,
            label: typeof f.label === 'string' ? f.label : undefined,
            placeholder: typeof f.placeholder === 'string' ? f.placeholder : undefined,
            options: Array.isArray(f.options) ? f.options.map(String) : undefined,
            stateKey: typeof f.stateKey === 'string' ? f.stateKey : undefined,
          })) as any)
      : []
    this.hasSubmit = (this.widget?.actions || []).some((a) => a.on === 'submit')
    this.submitLabel = typeof inputs.submitLabel === 'string' ? inputs.submitLabel : 'Submit'
  }

  private hydrateFromState(): void {
    const snapshot = this.state.getSnapshot()
    for (const f of this.fields) {
      const key = (f as any).stateKey || `form.${this.widget?.id}.${f.id}`
      if (snapshot[key] !== undefined) {
        this.values[f.id] = snapshot[key]
      } else if (this.values[f.id] === undefined) {
        this.values[f.id] = f.type === 'toggle' ? false : ''
      }
    }
  }

  async onSubmit(): Promise<void> {
    // Persist to state first.
    for (const f of this.fields) {
      const key = (f as any).stateKey || `form.${this.widget?.id}.${f.id}`
      this.state.set(key, this.values[f.id])
    }

    const cfg = this.widget
    if (!cfg?.actions) return
    const event = { values: { ...this.values } }
    for (const act of cfg.actions) {
      if (act.on === 'submit') {
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
    if (value.startsWith('$state.')) {
      const key = value.slice('$state.'.length)
      return this.state.getSnapshot()?.[key]
    }
    return value
  }
}

