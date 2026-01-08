import { Component, Input, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { addIcons } from 'ionicons'
import { micOutline, stopCircleOutline } from 'ionicons/icons'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { AdaosClient } from '../../core/adaos/adaos-client.service'
import { YDocService } from '../../y/ydoc.service'
import { BrowserSpeechRecognitionProvider } from '../../runtime/stt/providers/browser-sr.provider'
import { HubWavSttProvider } from '../../runtime/stt/providers/hub-wav.provider'
import { SttEvent, SttProvider } from '../../runtime/stt/stt.types'

@Component({
  selector: 'ada-voice-input-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `
    <div class="voice-input">
      <ion-button
        *ngIf="!pushToTalk; else ptt"
        (click)="toggleListening()"
        [color]="listening ? 'danger' : 'primary'"
      >
        <ion-icon slot="start" [name]="listening ? 'stop-circle-outline' : 'mic-outline'"></ion-icon>
        {{ listening ? stopLabel : startLabel }}
      </ion-button>
      <ng-template #ptt>
        <ion-button
          (pointerdown)="onPttDown($event)"
          (pointerup)="onPttUp($event)"
          (pointercancel)="onPttUp($event)"
          (pointerleave)="onPttUp($event)"
          (mousedown)="onPttDown($event)"
          (mouseup)="onPttUp($event)"
          (touchstart)="onPttDown($event)"
          (touchend)="onPttUp($event)"
          (click)="toggleListening()"
          [color]="listening ? 'danger' : 'primary'"
        >
          <ion-icon slot="start" [name]="listening ? 'stop-circle-outline' : 'mic-outline'"></ion-icon>
          {{ listening ? stopLabel : startLabel }}
        </ion-button>
      </ng-template>
      <div class="status" *ngIf="status">{{ status }}</div>
    </div>

    <div class="confirm" *ngIf="pendingText">
      <div class="confirm__label">Распознано:</div>
      <div class="confirm__text">{{ pendingText }}</div>
      <div class="confirm__actions">
        <ion-button size="small" (click)="confirmSend()" [disabled]="sending">Отправить</ion-button>
        <ion-button size="small" fill="outline" (click)="discard()" [disabled]="sending">Отмена</ion-button>
      </div>
    </div>
  `,
  styles: [
    `
      .voice-input {
        display: flex;
        align-items: center;
        gap: 10px;
        flex-wrap: wrap;
      }
      .status {
        font-size: 12px;
        opacity: 0.8;
      }
      .confirm {
        margin-top: 10px;
        padding: 10px 12px;
        border-radius: 12px;
        border: 1px solid rgba(var(--ion-text-color-rgb), 0.1);
        background: rgba(255, 255, 255, 0.04);
      }
      .confirm__label {
        font-size: 12px;
        opacity: 0.75;
        margin-bottom: 6px;
      }
      .confirm__text {
        font-size: 14px;
        line-height: 1.3;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
      }
      .confirm__actions {
        margin-top: 10px;
        display: flex;
        gap: 8px;
        justify-content: flex-end;
        flex-wrap: wrap;
      }
    `,
  ],
})
export class VoiceInputWidgetComponent implements OnInit, OnDestroy {
  @Input() widget!: WidgetConfig

  listening = false
  status = ''
  pendingText = ''
  sending = false
  private stickyStatus = false

  startLabel = 'Listen'
  stopLabel = 'Stop'
  lang = 'ru-RU'
  sendCommand?: string
  private sendMeta?: Record<string, any>
  private provider?: SttProvider
  private unsubProvider?: () => void
  pushToTalk = false
  autoSend = false

  constructor(private adaos: AdaosClient, private ydoc: YDocService) {}

  ngOnInit(): void {
    addIcons({ micOutline, stopCircleOutline })
    const inputs: any = this.widget?.inputs || {}
    this.startLabel = typeof inputs.startLabel === 'string' ? inputs.startLabel : 'Listen'
    this.stopLabel = typeof inputs.stopLabel === 'string' ? inputs.stopLabel : 'Stop'
    this.lang = typeof inputs.lang === 'string' ? inputs.lang : 'ru-RU'
    this.sendCommand = typeof inputs.sendCommand === 'string' ? inputs.sendCommand : undefined
    this.sendMeta = inputs && typeof inputs.meta === 'object' && inputs.meta ? { ...(inputs.meta as any) } : undefined

    const sttCfg: any = inputs?.stt || inputs || {}
    const providerId =
      (typeof sttCfg.sttProvider === 'string' ? sttCfg.sttProvider : undefined) ||
      (typeof sttCfg.provider === 'string' ? sttCfg.provider : undefined) ||
      'browser'
    this.pushToTalk = sttCfg.pushToTalk === true
    this.autoSend = sttCfg.autoSend === true
    const vad = sttCfg.vad === true
    const vadThreshold = typeof sttCfg.vadThreshold === 'number' ? sttCfg.vadThreshold : undefined
    const vadSilenceMs = typeof sttCfg.vadSilenceMs === 'number' ? sttCfg.vadSilenceMs : undefined

    this.provider?.destroy().catch(() => {})
    this.provider = this.createProvider(providerId, { vad, vadThreshold, vadSilenceMs })
    this.unsubProvider?.()
    this.unsubProvider = this.provider.onEvent((ev) => this.onSttEvent(ev))
  }

  ngOnDestroy(): void {
    this.unsubProvider?.()
    this.provider?.destroy().catch(() => {})
  }

  toggleListening(): void {
    if (this.listening) {
      void this.stopListening()
      return
    }
    void this.startListening()
  }

  onPttDown(ev: Event): void {
    ev.preventDefault()
    if (this.listening) return
    void this.startListening()
  }

  onPttUp(ev: Event): void {
    ev.preventDefault()
    if (!this.listening) return
    void this.stopListening()
  }

  private createProvider(
    providerId: string,
    opts: { vad?: boolean; vadThreshold?: number; vadSilenceMs?: number },
  ): SttProvider {
    const id = String(providerId || '').toLowerCase()
    if (id === 'hub' || id === 'hub-wav') {
      return new HubWavSttProvider({
        adaos: this.adaos,
        lang: this.lang,
        vad: opts.vad,
        vadThreshold: opts.vadThreshold,
        vadSilenceMs: opts.vadSilenceMs,
      })
    }
    return new BrowserSpeechRecognitionProvider({ lang: this.lang, interim: true })
  }

  private onSttEvent(ev: SttEvent): void {
    if (ev.type === 'state') {
      this.listening = ev.state === 'listening'
      if (ev.state === 'processing') this.status = 'Обработка…'
      if (ev.state === 'idle' && !this.pendingText && !this.stickyStatus) this.status = ''
      return
    }
    if (ev.type === 'partial') {
      this.stickyStatus = false
      this.status = ev.text
      return
    }
    if (ev.type === 'final') {
      const text = String(ev.text || '').trim()
      if (!text) return
      this.pendingText = text
      this.status = this.autoSend ? 'Отправка…' : ''
      this.stickyStatus = false
      if (this.autoSend) void this.sendRecognized(text)
      return
    }
    if (ev.type === 'error') {
      this.status = String(ev.message || 'Ошибка STT')
      this.stickyStatus = true
      this.listening = false
      try {
        const detail = (ev as any)?.detail
        if (detail) console.warn('[VoiceInput] STT error detail', detail)
      } catch {}
    }
  }

  private async startListening(): Promise<void> {
    if (!this.sendCommand) {
      this.status = 'sendCommand is not configured.'
      this.stickyStatus = true
      return
    }
    try {
      this.pendingText = ''
      this.status = ''
      this.stickyStatus = false
      await this.provider?.start()
    } catch (err) {
      this.status = 'Failed to start microphone.'
      this.stickyStatus = true
      this.listening = false
      try {
        this.provider?.destroy().catch(() => {})
      } catch {}
      this.provider = undefined
      throw err
    }
  }

  private async stopListening(): Promise<void> {
    try {
      await this.provider?.stop()
    } catch {}
  }

  discard(): void {
    this.pendingText = ''
    this.status = ''
    this.stickyStatus = false
  }

  async confirmSend(): Promise<void> {
    const text = String(this.pendingText || '').trim()
    if (!text) return
    await this.sendRecognized(text)
  }

  private async sendRecognized(text: string): Promise<void> {
    if (!this.sendCommand) return
    this.sending = true
    try {
      const ws = this.ydoc.getWebspaceId()
      const payload: any = { text, webspace_id: ws }
      if (this.sendMeta) payload._meta = { ...this.sendMeta }
      await this.adaos.sendEventsCommand(this.sendCommand, payload, 15000)
      setTimeout(() => {
        if (this.status === 'Отправка…') this.status = ''
      }, 1200)
      this.pendingText = ''
    } catch {
      this.status = 'Не удалось отправить сообщение.'
      this.stickyStatus = true
    } finally {
      this.sending = false
    }
  }
}

