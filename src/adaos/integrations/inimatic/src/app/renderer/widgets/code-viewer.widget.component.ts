import { Component, Input, OnChanges, OnDestroy, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { PageStateService } from '../../runtime/page-state.service'
import { Subscription } from 'rxjs'

@Component({
  selector: 'ada-code-viewer-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `
    <ion-card>
      <ion-card-header *ngIf="widget?.title">
        <ion-card-title>{{ widget.title }}</ion-card-title>
      </ion-card-header>
      <ion-card-content>
        <pre class="code-viewer">
<code [ngClass]="languageClass">{{ content }}</code>
        </pre>
      </ion-card-content>
    </ion-card>
  `,
  styles: [
    `
    .code-viewer {
      font-family: monospace;
      white-space: pre;
      overflow-x: auto;
      font-size: 0.85rem;
    }
    code.lang-python {
      color: #f8f8f2;
    }
    code.lang-json {
      color: #a6e22e;
    }
    code.lang-yaml {
      color: #66d9ef;
    }
    code.lang-markdown {
      color: #ffd866;
    }
    code.lang-text {
      color: #f8f8f2;
    }
  `,
  ],
})
export class CodeViewerWidgetComponent implements OnInit, OnChanges, OnDestroy {
  @Input() widget!: WidgetConfig

  content = ''
  languageClass = ''
  private currentPath?: string
  private stateSub?: Subscription

  constructor(
    private data: PageDataService,
    private state: PageStateService
  ) {}

  ngOnInit(): void {
    this.updateStream()
    this.stateSub = this.state.selectAll().subscribe((s) => {
      const nextPath = s['currentFilePath']
      if (nextPath !== this.currentPath) {
        this.currentPath = nextPath
        this.updateStream()
      }
    })
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.updateStream()
  }

  ngOnDestroy(): void {
    this.stateSub?.unsubscribe()
  }

  private updateStream(): void {
    const ds = this.widget?.dataSource
    if (!ds) {
      this.content = ''
      this.languageClass = ''
      return
    }
    const bindField: string =
      (this.widget.inputs && this.widget.inputs['bindField']) || 'content'
    const languageField: string =
      (this.widget.inputs && this.widget.inputs['languageField']) || 'language'

    this.data.load<any>(ds).subscribe({
      next: (value) => {
        try {
          const obj = value && typeof value === 'object' ? (value as any) : {}
          const next = obj[bindField]
          this.content = typeof next === 'string' ? next : ''
          const lang = obj[languageField]
          this.languageClass =
            typeof lang === 'string' && lang ? `lang-${lang}` : ''
        } catch {
          this.content = ''
          this.languageClass = ''
        }
      },
      error: () => {
        this.content = ''
        this.languageClass = ''
      },
    })
  }
}
