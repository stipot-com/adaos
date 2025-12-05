import { Component, Input, OnChanges, OnDestroy, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { Observable, Subscription } from 'rxjs'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { PageActionService } from '../../runtime/page-action.service'
import { FormsModule } from '@angular/forms'
import { PageStateService } from '../../runtime/page-state.service'

@Component({
  selector: 'ada-text-editor-widget',
  standalone: true,
  imports: [CommonModule, IonicModule, FormsModule],
  template: `
    <ion-card>
      <ion-card-header *ngIf="widget?.title">
        <ion-card-title>{{ widget.title }}</ion-card-title>
      </ion-card-header>
      <ion-card-content>
        <ion-textarea
          [autoGrow]="true"
          [(ngModel)]="current"
        ></ion-textarea>
        <div
          *ngIf="hasSaveAction"
          style="margin-top: 8px; display: flex; justify-content: flex-end;"
        >
          <ion-button size="small" (click)="onSave()" [disabled]="!isDirty()">
            Save
          </ion-button>
        </div>
      </ion-card-content>
    </ion-card>
  `,
})
export class TextEditorWidgetComponent implements OnInit, OnChanges, OnDestroy {
  @Input() widget!: WidgetConfig

  current = ''
  private baseline = ''
  private stateSub?: Subscription
  private stateDeps: string[] = []
  private lastState: Record<string, any> = {}

  constructor(
    private data: PageDataService,
    private actions: PageActionService,
    private state: PageStateService
  ) {}

  get hasSaveAction(): boolean {
    const acts = this.widget?.actions || []
    return acts.some((a) => a.on === 'save')
  }

  ngOnInit(): void {
    this.recomputeStateDeps()
    this.updateStream()
    this.stateSub = this.state.selectAll().subscribe(() => {
      this.onStateChanged()
    })
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.recomputeStateDeps()
    this.updateStream()
  }

  ngOnDestroy(): void {
    this.stateSub?.unsubscribe()
  }

  private updateStream(): void {
    const ds = this.widget?.dataSource
    if (!ds) {
      return
    }
    const bindField: string =
      (this.widget.inputs && this.widget.inputs['bindField']) || 'content'
    // Subscribe once to initialise baseline + current value.
    this.data.load<any>(ds).subscribe({
      next: (value) => {
        try {
          const next =
            value && typeof value === 'object' ? (value as any)[bindField] : undefined
          const text = typeof next === 'string' ? next : ''
          this.baseline = text
          this.current = text
        } catch {
          this.baseline = ''
          this.current = ''
        }
      },
      error: () => {
        this.baseline = ''
        this.current = ''
      },
    })
  }

  private recomputeStateDeps(): void {
    this.stateDeps = []
    const params = this.widget?.dataSource && (this.widget.dataSource as any).params
    if (!params || typeof params !== 'object') return
    for (const value of Object.values(params)) {
      if (typeof value === 'string' && value.startsWith('$state.')) {
        const key = value.slice('$state.'.length)
        if (key && !this.stateDeps.includes(key)) {
          this.stateDeps.push(key)
        }
      }
    }
    this.lastState = this.state.getSnapshot()
  }

  private onStateChanged(): void {
    if (!this.stateDeps.length) return
    const next = this.state.getSnapshot()
    const prev = this.lastState
    this.lastState = next
    for (const key of this.stateDeps) {
      if (prev[key] !== next[key]) {
        this.updateStream()
        break
      }
    }
  }

  isDirty(): boolean {
    return (this.current || '') !== (this.baseline || '')
  }

  async onSave(): Promise<void> {
    const cfg = this.widget
    if (!cfg?.actions || !cfg.actions.length) return
    const event = {
      content: this.current,
      ts: Date.now(),
    }
    for (const act of cfg.actions) {
      if (act.on === 'save') {
        await this.actions.handle(act, { event, widget: cfg })
      }
    }
  }
}
