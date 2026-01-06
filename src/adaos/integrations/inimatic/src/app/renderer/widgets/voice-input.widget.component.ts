import { Component, Input, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { addIcons } from 'ionicons'
import { micOutline, stopCircleOutline } from 'ionicons/icons'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { AdaosClient } from '../../core/adaos/adaos-client.service'
import { YDocService } from '../../y/ydoc.service'

@Component({
  selector: 'ada-voice-input-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `
    <div class="voice-input">
      <ion-button (click)="toggleListening()" [color]="listening ? 'danger' : 'primary'">
        <ion-icon slot="start" [name]="listening ? 'stop-circle-outline' : 'mic-outline'"></ion-icon>
        {{ listening ? stopLabel : startLabel }}
      </ion-button>
      <div class="status" *ngIf="status">{{ status }}</div>
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
    `,
  ],
})
export class VoiceInputWidgetComponent implements OnInit, OnDestroy {
  @Input() widget!: WidgetConfig

  listening = false
  status = ''

  startLabel = 'Listen'
  stopLabel = 'Stop'
  lang = 'ru-RU'
  sendCommand?: string

  private recognition?: any

  constructor(private adaos: AdaosClient, private ydoc: YDocService) {}

  ngOnInit(): void {
    addIcons({ micOutline, stopCircleOutline })
    const inputs: any = this.widget?.inputs || {}
    this.startLabel = typeof inputs.startLabel === 'string' ? inputs.startLabel : 'Listen'
    this.stopLabel = typeof inputs.stopLabel === 'string' ? inputs.stopLabel : 'Stop'
    this.lang = typeof inputs.lang === 'string' ? inputs.lang : 'ru-RU'
    this.sendCommand = typeof inputs.sendCommand === 'string' ? inputs.sendCommand : undefined
  }

  ngOnDestroy(): void {
    this.stopListening()
  }

  toggleListening(): void {
    if (this.listening) {
      this.stopListening()
      return
    }
    this.startListening()
  }

  private startListening(): void {
    if (!this.sendCommand) {
      this.status = 'sendCommand is not configured.'
      return
    }
    const SR = (globalThis as any).SpeechRecognition || (globalThis as any).webkitSpeechRecognition
    if (!SR) {
      this.status = 'SpeechRecognition is not available in this browser.'
      return
    }
    try {
      const rec = new SR()
      rec.lang = this.lang
      rec.interimResults = true
      rec.continuous = false
      rec.maxAlternatives = 1

      rec.onresult = (ev: any) => {
        try {
          let interim = ''
          let finalText = ''
          for (let i = ev.resultIndex; i < ev.results.length; i++) {
            const r = ev.results[i]
            const t = r?.[0]?.transcript || ''
            if (r?.isFinal) finalText += t
            else interim += t
          }
          this.status = interim.trim()
          const text = finalText.trim()
          if (text) this.sendRecognized(text)
        } catch {}
      }
      rec.onerror = () => {
        this.status = ''
        this.listening = false
      }
      rec.onend = () => {
        this.status = ''
        this.listening = false
      }
      this.recognition = rec
      this.status = ''
      this.listening = true
      rec.start()
    } catch {
      this.status = 'Failed to start microphone.'
      this.listening = false
    }
  }

  private stopListening(): void {
    this.listening = false
    this.status = ''
    try {
      this.recognition?.stop?.()
    } catch {}
    this.recognition = undefined
  }

  private async sendRecognized(text: string): Promise<void> {
    if (!this.sendCommand) return
    try {
      const ws = this.ydoc.getWebspaceId()
      await this.adaos.sendEventsCommand(this.sendCommand, { text, webspace_id: ws }, 15000)
    } catch {
      // ignore transient errors
    }
  }
}

