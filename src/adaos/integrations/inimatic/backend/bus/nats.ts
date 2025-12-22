import { connect, StringCodec, consumerOpts, createInbox } from 'nats'
import { randomUUID } from 'crypto'

let _nc: any = null
const sc = StringCodec()

export async function natsConnect(): Promise<void> {
  if (_nc) return
  const raw = (process.env['NATS_URL'] || '').trim()
  // Normalize to a core NATS TCP URL (ignore any WS/HTTP scheme and paths)
  let servers = 'nats://127.0.0.1:4222'
  try {
    if (raw) {
      const u = new URL(raw)
      const host = u.hostname || 'nats'
      const port = u.port || '4222'
      servers = `nats://${host}:${port}`
    } else {
      const host = process.env['NATS_HOST'] || 'nats'
      const port = process.env['NATS_PORT'] || '4222'
      servers = `nats://${host}:${port}`
    }
  } catch {
    // raw is not a full URL, accept as-is if it looks like host:port
    if (/^\w+:\/\//.test(raw)) {
      servers = raw
    } else if (raw) {
      servers = raw.startsWith('nats://') ? raw : `nats://${raw}`
    }
  }
  const user = process.env['NATS_USER'] || undefined
  const pass = process.env['NATS_PASS'] || undefined
  _nc = await connect({ servers, user, pass })
  try { console.log(`[nats] connected (alias-pub) servers=${servers}`) } catch {}
}

export async function publishIn(hub_id: string, payload: any): Promise<void> {
  await natsConnect()
  const subj = `tg.input.${hub_id}`
  let out = payload
  // Backward compat: if callers pass a legacy payload, wrap into io.input envelope.
  try {
    const isEnv = out && typeof out === 'object' && typeof out.kind === 'string' && out.kind === 'io.input' && out.payload
    if (!isEnv) {
      const bot_id = String((payload && typeof payload === 'object' ? (payload as any).bot_id : '') || process.env['BOT_ID'] || 'main-bot')
      const chat_id = String((payload && typeof payload === 'object' ? (payload as any).chat_id : '') || '')
      const tg_msg_id = Number((payload && typeof payload === 'object' ? (payload as any).tg_msg_id : 0) || 0)
      const text = String((payload && typeof payload === 'object' ? (payload as any).text : '') || '')
      const route = (payload && typeof payload === 'object') ? (payload as any).route : undefined

      out = {
        event_id: randomUUID().replace(/-/g, ''),
        kind: 'io.input',
        ts: new Date().toISOString(),
        dedup_key: `tg:${bot_id}:${chat_id}:${tg_msg_id}`,
        payload: {
          type: 'text',
          source: 'telegram',
          bot_id,
          hub_id,
          chat_id,
          user_id: chat_id,
          update_id: String(tg_msg_id),
          payload: { text, meta: { msg_id: tg_msg_id } },
          route,
        },
        meta: { bot_id, hub_id, trace_id: randomUUID().replace(/-/g, ''), retries: 0 },
      }
    }
  } catch { /* best effort */ }

  if (process.env['IO_DEBUG_PUBLISH_IN'] === '1') {
    try {
      const route = out && typeof out === 'object' ? (out as any).payload?.route : undefined
      console.log(`[nats] publishIn subj=${subj} via=${route?.via || ''} alias=${route?.alias || ''}`)
    } catch { /* ignore */ }
  }
  await _nc.publish(subj, sc.encode(JSON.stringify(out)))
}

export async function subscribeOut(handler: (payload: any) => Promise<void>): Promise<void> {
  await natsConnect()
  const js = _nc.jetstream()
  const opts = consumerOpts()
  opts.deliverTo(createInbox())
  opts.ackNone()
  const sub = await js.subscribe('io.tg.out', opts)
  ;(async () => {
    for await (const m of sub) {
      try { await handler(JSON.parse(sc.decode(m.data))) } catch { /* ignore */ }
    }
  })().catch(() => {})
}

export async function publishHubAlias(hub_id: string, alias: string): Promise<void> {
  await natsConnect()
  const subj = `hub.control.${hub_id}.alias`
  const payload = { alias }
  try {
    await _nc.publish(subj, sc.encode(JSON.stringify(payload)))
    try { console.log(`[nats] publish ${subj} ${JSON.stringify(payload)}`) } catch {}
  } catch (e) {
    try { console.warn(`[nats] publish failed ${subj}: ${String(e)}`) } catch {}
    throw e
  }
}
