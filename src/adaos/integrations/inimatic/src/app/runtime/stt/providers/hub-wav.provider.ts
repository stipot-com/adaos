import { AdaosClient } from '../../../core/adaos/adaos-client.service'
import { SttEvent, SttProvider } from '../stt.types'
import { encodeWavPcm16Mono, resampleToMono16k } from '../util/audio-utils'

type HubSttOptions = {
  adaos: AdaosClient
  lang?: string
  vad?: boolean
  vadThreshold?: number
  vadSilenceMs?: number
  maxMs?: number
}

export class HubWavSttProvider implements SttProvider {
  readonly id = 'hub-wav'

  private readonly adaos: AdaosClient
  private readonly lang: string
  private readonly vad: boolean
  private readonly vadThreshold: number
  private readonly vadSilenceMs: number
  private readonly maxMs: number

  private listeners = new Set<(ev: SttEvent) => void>()
  private stream?: MediaStream
  private ctx?: AudioContext
  private processor?: ScriptProcessorNode
  private source?: MediaStreamAudioSourceNode
  private chunks: Float32Array[] = []
  private startedAt = 0
  private lastVoiceAt = 0
  private hadVoice = false
  private stopRequested = false
  private hardStopTimer?: any

  constructor(opts: HubSttOptions) {
    this.adaos = opts.adaos
    this.lang = opts.lang || 'ru-RU'
    this.vad = opts.vad === true
    this.vadThreshold = typeof opts.vadThreshold === 'number' ? opts.vadThreshold : 0.01
    this.vadSilenceMs = typeof opts.vadSilenceMs === 'number' ? opts.vadSilenceMs : 900
    this.maxMs = typeof opts.maxMs === 'number' ? opts.maxMs : 15000
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

  async start(): Promise<void> {
    if (this.stream) return
    this.stopRequested = false
    this.startedAt = Date.now()
    this.lastVoiceAt = this.startedAt
    this.hadVoice = false
    this.chunks = []
    this.emit({ type: 'state', state: 'listening' })
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (err) {
      this.emit({ type: 'error', message: 'Microphone permission denied.', detail: err })
      this.emit({ type: 'state', state: 'idle' })
      this.stream = undefined
      return
    }

    try {
      this.ctx = new (globalThis.AudioContext || (globalThis as any).webkitAudioContext)()
      this.source = this.ctx.createMediaStreamSource(this.stream)
      this.processor = this.ctx.createScriptProcessor(4096, 1, 1)
      this.source.connect(this.processor)
      // Do not output audio; still needs to be connected in some browsers.
      this.processor.connect(this.ctx.destination)
      this.processor.onaudioprocess = (ev: AudioProcessingEvent) => {
        try {
          const data = ev.inputBuffer.getChannelData(0)
          this.chunks.push(new Float32Array(data))
          const now = Date.now()
          if (this.vad) {
            const rms = computeRms(data)
            if (rms >= this.vadThreshold) {
              this.hadVoice = true
              this.lastVoiceAt = now
            } else if (this.hadVoice && now - this.lastVoiceAt >= this.vadSilenceMs) {
              void this.stop()
            }
          }
          if (now - this.startedAt >= this.maxMs) {
            void this.stop()
          }
        } catch {}
      }
      // Hard stop even if ScriptProcessor callbacks are not delivered (some browsers).
      try {
        clearTimeout(this.hardStopTimer)
      } catch {}
      this.hardStopTimer = setTimeout(() => {
        void this.stop()
      }, this.maxMs + 250)
    } catch (err) {
      this.emit({ type: 'error', message: 'Failed to start audio capture.', detail: err })
      await this.stop()
    }
  }

  async stop(): Promise<void> {
    if (this.stopRequested) return
    this.stopRequested = true
    try {
      clearTimeout(this.hardStopTimer)
    } catch {}
    this.hardStopTimer = undefined
    if (!this.stream) {
      await this.cleanupCapture()
      this.emit({ type: 'state', state: 'idle' })
      return
    }
    this.emit({ type: 'state', state: 'processing' })
    const sampleRate = this.ctx?.sampleRate || 48000
    const merged = mergeFloat32(this.chunks)
    const pcm16 = resampleToMono16k(merged, sampleRate)
    const wav = encodeWavPcm16Mono(pcm16, 16000)

    await this.cleanupCapture()

    try {
      const base = this.adaos.getBaseUrl().replace(/\/$/, '')
      const url = `${base}/api/stt/transcribe`

      const wavB64 = await blobToBase64(wav)
      const body = JSON.stringify({ audio_b64: wavB64, lang: this.lang })
      const resp = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...this.adaos.getAuthHeaders(),
        },
        body,
      })
      if (!resp.ok) {
        const detail = await resp.text().catch(() => '')
        throw new Error(`hub stt failed: ${resp.status} ${detail}`.trim())
      }
      const raw = await resp.text().catch(() => '')
      if (!raw.trim()) {
        this.emit({ type: 'error', message: 'Empty STT response from hub.' })
        return
      }
      let res: any = undefined
      try {
        res = JSON.parse(raw)
      } catch (err) {
        this.emit({ type: 'error', message: 'Invalid STT response from hub.', detail: raw })
        return
      }
      const text = String(res?.text || '').trim()
      if (text) this.emit({ type: 'final', text })
      else this.emit({ type: 'error', message: 'No speech recognized.' })
    } catch (err) {
      this.emit({ type: 'error', message: 'Hub STT request failed.', detail: err })
    } finally {
      this.emit({ type: 'state', state: 'idle' })
    }
  }

  private async cleanupCapture(): Promise<void> {
    try {
      clearTimeout(this.hardStopTimer)
    } catch {}
    this.hardStopTimer = undefined
    try {
      this.processor && (this.processor.onaudioprocess = null as any)
    } catch {}
    try {
      this.processor?.disconnect()
    } catch {}
    try {
      this.source?.disconnect()
    } catch {}
    try {
      await this.ctx?.close?.()
    } catch {}
    try {
      this.stream?.getTracks?.().forEach((t) => t.stop())
    } catch {}
    this.processor = undefined
    this.source = undefined
    this.ctx = undefined
    this.stream = undefined
  }

  async destroy(): Promise<void> {
    await this.cleanupCapture()
    this.listeners.clear()
  }
}

function computeRms(buf: Float32Array): number {
  let sum = 0
  for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i]
  return Math.sqrt(sum / Math.max(1, buf.length))
}

function mergeFloat32(chunks: Float32Array[]): Float32Array {
  const total = chunks.reduce((acc, c) => acc + c.length, 0)
  const out = new Float32Array(total)
  let offset = 0
  for (const c of chunks) {
    out.set(c, offset)
    offset += c.length
  }
  return out
}

async function blobToBase64(blob: Blob): Promise<string> {
  const buf = await blob.arrayBuffer()
  const bytes = new Uint8Array(buf)
  let binary = ''
  // chunk to avoid call stack limits
  const chunkSize = 0x8000
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const sub = bytes.subarray(i, i + chunkSize)
    binary += String.fromCharCode(...sub)
  }
  return btoa(binary)
}
