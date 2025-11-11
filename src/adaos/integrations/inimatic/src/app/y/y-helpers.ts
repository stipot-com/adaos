import * as Y from 'yjs'

export function observeDeep(yobj: any, cb: () => void) {
  const handler = () => cb()
  if (yobj && typeof (yobj as any).observeDeep === 'function') {
    ;(yobj as any).observeDeep(handler)
    return () => {
      try { (yobj as any).unobserveDeep(handler) } catch {}
    }
  }
  // fallback: one-shot
  cb()
  return () => {}
}

