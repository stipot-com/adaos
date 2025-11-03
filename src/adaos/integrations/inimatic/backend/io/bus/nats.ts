import { connect, StringCodec } from 'nats'
import type { NatsConnection, Subscription } from 'nats'

export type NatsOpts = {
  servers: string
  user?: string
  pass?: string
}

function toOpts(urlOrOpts: string | NatsOpts): NatsOpts {
  if (typeof urlOrOpts !== 'string') return urlOrOpts
  const u = new URL(urlOrOpts.trim())
  const proto = u.protocol && u.protocol !== ':' ? u.protocol : 'nats:'
  const host = u.hostname || 'nats'
  const port = u.port || '4222'
  const servers = `${proto}//${host}:${port}`
  const user = u.username || undefined
  const pass = u.password ? decodeURIComponent(u.password) : undefined
  return { servers, user, pass }
}

export class NatsBus {
  private nc!: NatsConnection
  private sc = StringCodec()
  private opts: NatsOpts

  constructor(urlOrOpts: string | NatsOpts) {
    this.opts = toOpts(urlOrOpts)
  }

  async connect() {
    // Core NATS only (no JetStream requirement)
    this.nc = await connect({ servers: this.opts.servers, user: this.opts.user, pass: this.opts.pass })
  }

  async publish_input(hub_id: string, envelope: any) {
    await this.nc.publish(`tg.input.${hub_id}`, this.sc.encode(JSON.stringify(envelope)))
  }

  async subscribe_output(bot_id: string, handler: (subject: string, data: Uint8Array) => Promise<void>) {
    const subj = `tg.output.${bot_id}.>`
    const sub: Subscription = this.nc.subscribe(subj)
    ;(async () => { for await (const m of sub) await handler(m.subject, m.data) })().catch(() => {})
    return sub
  }

  // Backward-compat: some hubs publish to a legacy subject `io.tg.out`
  async subscribe_compat_out(handler: (subject: string, data: Uint8Array) => Promise<void>) {
    const sub: Subscription = this.nc.subscribe('io.tg.out')
    ;(async () => { for await (const m of sub) await handler(m.subject, m.data) })().catch(() => {})
    return sub
  }

  async publish_dlq(stage: string, payload: any) {
    await this.nc.publish(`tg.dlq.${stage}`, this.sc.encode(JSON.stringify(payload)))
  }

  async publish_subject(subject: string, payload: any) {
    await this.nc.publish(subject, this.sc.encode(JSON.stringify(payload)))
  }

  async publishSubject(subject: string, payload: any) {
    return this.publish_subject(subject, payload)
  }
}

