import { Component, Input, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { Subscription } from 'rxjs'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { AdaosClient } from '../../core/adaos/adaos-client.service'
import { YDocService } from '../../y/ydoc.service'

type TeacherState = {
  items?: any[]
  revisions?: any[]
  dataset?: any[]
  candidates?: any[]
  llm_logs?: any[]
}

@Component({
  selector: 'ada-nlu-teacher-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `
      <div class="wrap">
      <div class="header">
        <h2>{{ widget.title || 'NLU Teacher' }}</h2>
        <ion-button size="small" fill="outline" (click)="reload()">Reload</ion-button>
      </div>

      <ion-card *ngIf="error">
        <ion-card-header>
          <ion-card-title>Error</ion-card-title>
        </ion-card-header>
        <ion-card-content>
          <div class="err">{{ error }}</div>
        </ion-card-content>
      </ion-card>

      <ion-card>
        <ion-card-header>
          <ion-card-title>Revisions</ion-card-title>
          <ion-card-subtitle *ngIf="revisions.length">Latest first</ion-card-subtitle>
        </ion-card-header>
        <ion-card-content>
          <div class="empty" *ngIf="!revisions.length">No revisions</div>

          <div class="rev" *ngFor="let r of revisions">
            <div class="row">
              <ion-badge color="medium">{{ r.status || 'unknown' }}</ion-badge>
              <div class="text">{{ r.text || r.request?.text || '' }}</div>
            </div>
            <div class="meta">
              <div *ngIf="r.reason">reason: {{ r.reason }}</div>
              <div *ngIf="r.request_id">request_id: {{ r.request_id }}</div>
              <div *ngIf="r.proposal?.intent">intent: {{ r.proposal.intent }}</div>
            </div>
            <div class="actions">
              <ion-button
                size="small"
                (click)="applyRevision(r)"
                [disabled]="!canApply(r)"
              >
                Apply
              </ion-button>
            </div>
          </div>
        </ion-card-content>
      </ion-card>

      <ion-card>
        <ion-card-header>
          <ion-card-title>Candidates</ion-card-title>
          <ion-card-subtitle *ngIf="candidates.length">Latest first</ion-card-subtitle>
        </ion-card-header>
        <ion-card-content>
          <div class="empty" *ngIf="!candidates.length">No candidates</div>
          <div class="rev" *ngFor="let c of candidates">
            <div class="row">
              <ion-badge color="secondary">{{ c.kind || 'unknown' }}</ion-badge>
              <div class="text">{{ c.text || '' }}</div>
            </div>
            <div class="meta">
              <div *ngIf="c.candidate?.name">name: {{ c.candidate.name }}</div>
              <div *ngIf="c.status">status: {{ c.status }}</div>
              <div *ngIf="c.request_id">request_id: {{ c.request_id }}</div>
            </div>
          </div>
        </ion-card-content>
      </ion-card>

      <ion-card>
        <ion-card-header>
          <ion-card-title>LLM Logs</ion-card-title>
          <ion-card-subtitle>Prompt / response</ion-card-subtitle>
        </ion-card-header>
        <ion-card-content>
          <div class="empty" *ngIf="!llmLogs.length">No LLM logs</div>

          <ion-accordion-group *ngIf="llmLogs.length">
            <ion-accordion *ngFor="let l of llmLogs" [value]="l.id">
              <ion-item slot="header">
                <ion-label>
                  <div class="log-title">
                    <span class="mono">{{ l.id }}</span>
                    <ion-badge color="tertiary" *ngIf="l.status">{{ l.status }}</ion-badge>
                  </div>
                  <div class="log-sub">
                    <span *ngIf="l.request_id" class="mono">{{ l.request_id }}</span>
                    <span *ngIf="l.duration_s != null">· {{ l.duration_s | number: '1.2-2' }}s</span>
                    <span *ngIf="l.model">· {{ l.model }}</span>
                  </div>
                </ion-label>
              </ion-item>

              <div slot="content" class="log-body">
                <div *ngIf="l.error" class="err">error: {{ l.error }}</div>
                <div class="block">
                  <div class="label">Request</div>
                  <pre>{{ l.request | json }}</pre>
                </div>
                <div class="block">
                  <div class="label">Response</div>
                  <pre>{{ l.response | json }}</pre>
                </div>
              </div>
            </ion-accordion>
          </ion-accordion-group>
        </ion-card-content>
      </ion-card>
    </div>
  `,
  styles: [
    `
      .wrap {
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
      }
      h2 {
        margin: 0;
        font-size: 16px;
        font-weight: 600;
      }
      .rev {
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 12px;
        padding: 10px 10px;
        margin-bottom: 10px;
      }
      .row {
        display: flex;
        align-items: center;
        gap: 10px;
      }
      .text {
        font-size: 13px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .meta {
        margin-top: 6px;
        opacity: 0.75;
        font-size: 12px;
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
      }
      .actions {
        margin-top: 10px;
        display: flex;
        justify-content: flex-end;
      }
      .empty {
        opacity: 0.7;
        font-size: 13px;
      }
      .mono {
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New',
          monospace;
      }
      .log-title {
        display: flex;
        align-items: center;
        gap: 10px;
      }
      .log-sub {
        font-size: 12px;
        opacity: 0.7;
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
      }
      .log-body {
        padding: 10px 12px;
      }
      .block .label {
        font-size: 12px;
        opacity: 0.75;
        margin-bottom: 6px;
      }
      pre {
        margin: 0;
        padding: 10px;
        border-radius: 10px;
        background: rgba(0, 0, 0, 0.06);
        overflow: auto;
        font-size: 12px;
        line-height: 1.25;
      }
      :host-context(body.dark) pre {
        background: rgba(255, 255, 255, 0.06);
      }
      .err {
        color: var(--ion-color-danger);
        margin-bottom: 10px;
        font-size: 13px;
      }
    `,
  ],
})
export class NluTeacherWidgetComponent implements OnInit, OnDestroy {
  @Input() widget!: WidgetConfig

  private sub?: Subscription
  private lastValue?: TeacherState

  revisions: any[] = []
  llmLogs: any[] = []
  candidates: any[] = []
  error?: string

  constructor(private data: PageDataService, private adaos: AdaosClient, private ydoc: YDocService) {}

  ngOnInit(): void {
    this.reload()
  }

  ngOnDestroy(): void {
    this.sub?.unsubscribe()
  }

  reload(): void {
    this.sub?.unsubscribe()
    this.error = undefined
    const src =
      this.widget?.dataSource ||
      ({
        kind: 'y',
        path: 'data/nlu_teacher',
      } as any)
    const stream = this.data.load<any>(src)
    this.sub = stream.subscribe({
      next: (value) => {
        this.lastValue = (value && typeof value === 'object' ? value : {}) as TeacherState
        this.revisions = this.normalizeList(this.lastValue?.revisions).slice(0, 100)
        this.candidates = this.normalizeList(this.lastValue?.candidates).slice(0, 50)
        this.llmLogs = this.normalizeList(this.lastValue?.llm_logs).slice(0, 50)
      },
      error: (err) => {
        this.error = String(err?.message || err || 'unknown error')
        this.revisions = []
        this.candidates = []
        this.llmLogs = []
      },
    })
  }

  canApply(rev: any): boolean {
    const s = (rev?.status || '').toString()
    return s === 'proposed'
  }

  async applyRevision(rev: any): Promise<void> {
    if (!this.canApply(rev)) return
    try {
      const ws = this.ydoc.getWebspaceId()
      const payload: any = {
        webspace_id: ws,
        request_id: rev?.request_id,
        revision_id: rev?.id,
      }
      await this.adaos.sendEventsCommand('nlp.teacher.revision.apply', payload, 15000)
    } catch {
      // ignore; state will resync
    }
  }

  private normalizeList(value: any): any[] {
    if (!Array.isArray(value)) return []
    return value
      .filter((x) => x && typeof x === 'object')
      .slice()
      .sort((a, b) => Number(b?.ts || 0) - Number(a?.ts || 0))
  }
}
