import { connect, StringCodec, consumerOpts, createInbox } from 'nats'

let _nc: any = null
const sc = StringCodec()

export async function natsConnect(): Promise<void> {
  if (_nc) return
  const url = (process.env['NATS_URL'] || 'nats://127.0.0.1:4222').trim()
  const user = process.env['NATS_USER'] || undefined
  const pass = process.env['NATS_PASS'] || undefined
  _nc = await connect({ servers: url, user, pass })
  try { console.log(`[nats] connected (alias-pub) url=${url}`) } catch {}
}

export async function publishIn(hub_id: string, payload: any): Promise<void> {
  await natsConnect()
  const subj = `tg.input.${hub_id}`
  await _nc.publish(subj, sc.encode(JSON.stringify(payload)))
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
