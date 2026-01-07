export function resampleToMono16k(float32: Float32Array, srcRate: number): Int16Array {
  const targetRate = 16000
  if (!float32.length) return new Int16Array()
  if (!srcRate || srcRate <= 0) srcRate = targetRate
  if (srcRate === targetRate) {
    return floatTo16BitPCM(float32)
  }
  const ratio = srcRate / targetRate
  const newLength = Math.max(1, Math.round(float32.length / ratio))
  const out = new Float32Array(newLength)
  for (let i = 0; i < newLength; i++) {
    const idx = i * ratio
    const idx0 = Math.floor(idx)
    const idx1 = Math.min(float32.length - 1, idx0 + 1)
    const frac = idx - idx0
    out[i] = float32[idx0] * (1 - frac) + float32[idx1] * frac
  }
  return floatTo16BitPCM(out)
}

export function floatTo16BitPCM(float32: Float32Array): Int16Array {
  const out = new Int16Array(float32.length)
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]))
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff
  }
  return out
}

export function encodeWavPcm16Mono(pcm: Int16Array, sampleRate = 16000): Blob {
  const buffer = new ArrayBuffer(44 + pcm.length * 2)
  const view = new DataView(buffer)

  // RIFF header
  writeString(view, 0, 'RIFF')
  view.setUint32(4, 36 + pcm.length * 2, true)
  writeString(view, 8, 'WAVE')

  // fmt chunk
  writeString(view, 12, 'fmt ')
  view.setUint32(16, 16, true) // chunk size
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, 1, true) // mono
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true) // byte rate
  view.setUint16(32, 2, true) // block align
  view.setUint16(34, 16, true) // bits per sample

  // data chunk
  writeString(view, 36, 'data')
  view.setUint32(40, pcm.length * 2, true)

  let offset = 44
  for (let i = 0; i < pcm.length; i++, offset += 2) {
    view.setInt16(offset, pcm[i], true)
  }

  return new Blob([buffer], { type: 'audio/wav' })
}

function writeString(view: DataView, offset: number, s: string): void {
  for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i))
}

