export type SttEvent =
  | { type: 'state'; state: 'idle' | 'listening' | 'processing' }
  | { type: 'partial'; text: string }
  | { type: 'final'; text: string }
  | { type: 'error'; message: string; detail?: any }

export interface SttProvider {
  readonly id: string
  start(): Promise<void>
  stop(): Promise<void>
  destroy(): Promise<void>
  onEvent(cb: (ev: SttEvent) => void): () => void
}

