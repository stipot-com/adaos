import { connect, StringCodec, consumerOpts, createInbox, RetentionPolicy, DiscardPolicy, StorageType } from 'nats'
import type { NatsConnection, JetStreamManager } from 'nats'

export type NatsOpts = {
	servers: string
	user?: string
	pass?: string
}

function toOpts(urlOrOpts: string | NatsOpts): NatsOpts {
	if (typeof urlOrOpts !== 'string') return urlOrOpts
	// парсим строку-URL и делаем поля сервера/учётки отдельно
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

	// перегрузка: принимаем и строку, и объект
	constructor(urlOrOpts: string | NatsOpts) {
		this.opts = toOpts(urlOrOpts)
	}

	async connect() {
		// ключевое — auth не в URL, а отдельными полями
		this.nc = await connect({
			servers: this.opts.servers,
			user: this.opts.user,
			pass: this.opts.pass,
		})

		const jsm: JetStreamManager = await this.nc.jetstreamManager()
		const ensure = async (name: string, subjects: string[]) => {
			try {
				await jsm.streams.add({
					name,
					subjects,
					retention: RetentionPolicy.Limits,
					discard: DiscardPolicy.Old,
					storage: StorageType.File,
				})
			} catch { /* already exists */ }
		}
		await ensure('TG_INPUT', ['tg.input.*'])
		await ensure('TG_OUTPUT', ['tg.output.>'])
		await ensure('TG_DLQ', ['tg.dlq.*'])
		await ensure('SUBNET_INPUT', ['subnet.input.*.inbox'])
	}

	async publish_input(hub_id: string, envelope: any) {
		const js = this.nc.jetstream()
		await js.publish(`tg.input.${hub_id}`, this.sc.encode(JSON.stringify(envelope)))
	}

	async subscribe_output(bot_id: string, handler: (subject: string, data: Uint8Array) => Promise<void>) {
		const js = this.nc.jetstream()

		// Имя durable: только буквы/цифры/подчёркивание/дефис
		const safeBot = bot_id.replace(/[^A-Za-z0-9_-]/g, '_')
		const durable = `tg_out_${safeBot}`           // без точки
		const subj = `tg.output.${bot_id}.>`       // subject с точками допустим

		const opts = consumerOpts()
		opts.durable(durable)
		opts.deliverTo(createInbox())
		opts.ackNone()

		const sub = await js.subscribe(subj, opts)
			; (async () => {
				for await (const m of sub) await handler(m.subject, m.data)
			})().catch(() => { })
		return sub
	}

	async publish_dlq(stage: string, payload: any) {
		const js = this.nc.jetstream()
		await js.publish(`tg.dlq.${stage}`, this.sc.encode(JSON.stringify(payload)))
	}

	async publish_subject(subject: string, payload: any) {
		const js = this.nc.jetstream()
		await js.publish(subject, this.sc.encode(JSON.stringify(payload)))
	}

	async publishSubject(subject: string, payload: any) {
		return this.publish_subject(subject, payload)
	}
}
