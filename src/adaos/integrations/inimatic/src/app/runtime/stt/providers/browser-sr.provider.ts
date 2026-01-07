import { SttEvent, SttProvider } from '../stt.types'

export class BrowserSpeechRecognitionProvider implements SttProvider {
  readonly id = 'browser-sr'

  private readonly lang: string
  private readonly interim: boolean
  private recognition?: any
  private listeners = new Set<(ev: SttEvent) => void>()

  constructor(opts: { lang?: string; interim?: boolean }) {
    this.lang = opts.lang || 'ru-RU'
    this.interim = opts.interim !== false
  }

  onEvent(cb: (ev: SttEvent) => void): () => void {
    this.listeners.add(cb)
    return () => this.listeners.delete(cb)
  }

  private emit(ev: SttEvent): void {
    for (const cb of this.listeners) {
      try {
        cb(ev)
      } catch {}
    }
  }

  private createRecognition(): any {
    const SR = (globalThis as any).SpeechRecognition || (globalThis as any).webkitSpeechRecognition
    if (!SR) return null
    const rec = new SR()
    rec.lang = this.lang
    rec.interimResults = this.interim
    rec.continuous = false
    rec.maxAlternatives = 1
    return rec
  }

  async start(): Promise<void> {
    if (this.recognition) return
    const rec = this.createRecognition()
    if (!rec) {
      this.emit({ type: 'error', message: 'SpeechRecognition is not available in this browser.' })
      return
    }
    this.recognition = rec
    this.emit({ type: 'state', state: 'listening' })

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
        const interimTrimmed = interim.trim()
        if (interimTrimmed) this.emit({ type: 'partial', text: interimTrimmed })
        const finalTrimmed = finalText.trim()
        if (finalTrimmed) this.emit({ type: 'final', text: finalTrimmed })
      } catch {}
    }
    rec.onerror = (err: any) => {
      this.emit({ type: 'error', message: 'SpeechRecognition error', detail: err })
      this.emit({ type: 'state', state: 'idle' })
      this.recognition = undefined
    }
    rec.onend = () => {
      this.emit({ type: 'state', state: 'idle' })
      this.recognition = undefined
    }

    try {
      rec.start()
    } catch (err) {
      this.emit({ type: 'error', message: 'Failed to start microphone.', detail: err })
      this.emit({ type: 'state', state: 'idle' })
      this.recognition = undefined
    }
  }

  async stop(): Promise<void> {
    try {
      this.recognition?.stop?.()
    } catch {}
  }

  async destroy(): Promise<void> {
    await this.stop()
    this.recognition = undefined
    this.listeners.clear()
  }
}

