import { Component, Input, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { FormsModule } from '@angular/forms'
import { Subscription } from 'rxjs'
import { addIcons } from 'ionicons'
import { sendOutline } from 'ionicons/icons'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'
import { AdaosClient } from '../../core/adaos/adaos-client.service'
import { YDocService } from '../../y/ydoc.service'
import { coerceChatState, WebIOChatMessage } from '../../runtime/webio-contracts'

@Component({
  selector: 'ada-chat-widget',
  standalone: true,
  imports: [CommonModule, IonicModule, FormsModule],
  template: `
    <div class="chat">
      <div class="messages" [attr.data-align-right]="alignRightFrom">
        <div
          class="msg"
          *ngFor="let m of messages"
          [class.me]="(m.from || '') === alignRightFrom"
        >
          <div class="bubble">{{ m.text }}</div>
        </div>
        <div class="hint" *ngIf="!messages.length && hint">{{ hint }}</div>
      </div>

      <div class="composer" *ngIf="sendCommand">
        <ion-input
          [(ngModel)]="draft"
          [placeholder]="placeholder"
          (keyup.enter)="sendDraft()"
        ></ion-input>
        <ion-button (click)="sendDraft()" [disabled]="!draft.trim()">
          <ion-icon slot="icon-only" name="send-outline"></ion-icon>
        </ion-button>
      </div>
    </div>
  `,
  styles: [
    `
      .chat {
        display: flex;
        flex-direction: column;
        gap: 10px;
        min-height: 60vh;
      }
      .messages {
        flex: 1;
        display: flex;
        flex-direction: column;
        gap: 10px;
        overflow: auto;
        padding: 4px 2px;
      }
      .msg {
        display: flex;
        justify-content: flex-start;
      }
      .msg.me {
        justify-content: flex-end;
      }
      .bubble {
        max-width: min(78%, 520px);
        padding: 10px 12px;
        border-radius: 14px;
        background: rgba(0, 0, 0, 0.06);
        color: var(--ion-text-color);
        line-height: 1.25;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
      }
      :host-context(body.dark) .bubble {
        background: rgba(255, 255, 255, 0.08);
      }
      .msg.me .bubble {
        background: rgba(var(--ion-color-primary-rgb), 0.16);
      }
      .composer {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .composer ion-input {
        flex: 1;
      }
      .hint {
        font-size: 13px;
        opacity: 0.7;
        text-align: center;
        padding: 18px 0;
      }
    `,
  ],
})
export class ChatWidgetComponent implements OnInit, OnDestroy {
  @Input() widget!: WidgetConfig

  messages: WebIOChatMessage[] = []
  draft = ''

  alignRightFrom = 'user'
  hint = ''
  placeholder = 'Type a message…'

  private dataSub?: Subscription
  private openCommand?: string
  sendCommand?: string
  private sendMeta?: Record<string, any>
  private autoSpeakFrom?: string
  private autoSpeakLang?: string
  private spokenIds = new Set<string>()

  constructor(
    private data: PageDataService,
    private adaos: AdaosClient,
    private ydoc: YDocService,
  ) {}

  ngOnInit(): void {
    addIcons({ sendOutline })

    const inputs: any = this.widget?.inputs || {}
    this.openCommand = typeof inputs.openCommand === 'string' ? inputs.openCommand : undefined
    this.sendCommand = typeof inputs.sendCommand === 'string' ? inputs.sendCommand : undefined
    this.alignRightFrom = typeof inputs.alignRightFrom === 'string' ? inputs.alignRightFrom : 'user'
    this.hint = typeof inputs.hint === 'string' ? inputs.hint : ''
    this.placeholder = typeof inputs.placeholder === 'string' ? inputs.placeholder : 'Type a message…'
    this.sendMeta = inputs && typeof inputs.meta === 'object' && inputs.meta ? { ...(inputs.meta as any) } : undefined

    if (inputs.autoSpeak === true || typeof inputs.autoSpeakFrom === 'string') {
      this.autoSpeakFrom = typeof inputs.autoSpeakFrom === 'string' ? inputs.autoSpeakFrom : 'hub'
      this.autoSpeakLang = typeof inputs.autoSpeakLang === 'string' ? inputs.autoSpeakLang : 'ru-RU'
    }

    if (this.openCommand) {
      try {
        const ws = this.ydoc.getWebspaceId()
        this.adaos.sendEventsCommand(this.openCommand, { webspace_id: ws }).catch(() => {})
      } catch {}
    }

    const stream = this.data.load<any>(this.widget?.dataSource)
    if (stream) {
      this.dataSub = stream.subscribe((value) => {
        const next = coerceChatState(value).messages
        try {
          // eslint-disable-next-line no-console
          console.log('[ChatWidget] update', this.widget?.id, 'len=', next.length, next)
        } catch {}
        this.messages = next
        this.maybeSpeakNew()
        setTimeout(() => {
          try {
            const el = document.querySelector('.chat .messages') as HTMLElement | null
            if (el) el.scrollTop = el.scrollHeight
          } catch {}
        }, 0)
      })
    }
  }

  ngOnDestroy(): void {
    this.dataSub?.unsubscribe()
  }

  get sendCommandResolved(): string | undefined {
    return this.sendCommand
  }

  async sendDraft(): Promise<void> {
    if (!this.sendCommandResolved) return
    const text = (this.draft || '').trim()
    if (!text) return
    this.draft = ''
    try {
      const ws = this.ydoc.getWebspaceId()
      const payload: any = { text, webspace_id: ws }
      if (this.sendMeta) payload._meta = { ...this.sendMeta }
      await this.adaos.sendEventsCommand(this.sendCommandResolved, payload, 15000)
    } catch {
      // ignore transient errors; state will resync on next open
    }
  }

  private maybeSpeakNew(): void {
    if (!this.autoSpeakFrom) return
    const latest = [...this.messages]
      .reverse()
      .find((m) => (m.from || '') === this.autoSpeakFrom && m.text)
    if (!latest?.id || this.spokenIds.has(latest.id)) return
    this.spokenIds.add(latest.id)
    try {
      const synth: SpeechSynthesis | undefined = (globalThis as any).speechSynthesis
      if (!synth) return
      const u = new SpeechSynthesisUtterance(latest.text)
      if (this.autoSpeakLang) u.lang = this.autoSpeakLang
      synth.cancel()
      synth.speak(u)
    } catch {}
  }
}
