import { handleCommand } from './commands.js'
import { resolveTarget, stripExplicitAlias } from './resolver.js'
import { ensureSchema, logMessage, upsertBinding, listBindings, setSession } from '../../db/tg.repo.js'
import { publishIn, subscribeOut } from '../../bus/nats.js'
import { sendToTelegram } from './outbox.js'
import { log } from '../../logging/routing.js'
import { pairConfirm, tgLinkSet } from '../pairing/store.js'

type Update = any

let _inited = false

export async function initTgRouting(): Promise<void> {
	if (_inited) return
	await ensureSchema()
	await subscribeOut(async (msg) => {
		try {
			const chat_id = Number(msg.chat_id)
			const text = String(msg.text || '')
			const alias = typeof msg.alias === 'string' ? msg.alias : undefined
			const reply = msg.reply_to_tg_msg_id ? Number(msg.reply_to_tg_msg_id) : undefined
			await sendToTelegram({ chat_id, text, alias, reply_to_message_id: reply })
		} catch (e) {
			log.warn({ err: String(e) }, 'outbox deliver failed')
		}
	})
	_inited = true
}

export function toCtx(bot_id: string, update: Update): { chat_id: number, text?: string, msg_id: number, reply_to_msg_id?: number, topic_id?: number, is_command: boolean } {
	const msg = update?.message || update?.edited_message || update?.callback_query?.message || {}
	const text: string | undefined = update?.message?.text || update?.edited_message?.text || update?.callback_query?.data
	const chat_id: number = Number(msg?.chat?.id)
	const msg_id: number = Number(msg?.message_id)
	const reply_to_msg_id: number | undefined = msg?.reply_to_message?.message_id ? Number(msg.reply_to_message.message_id) : undefined
	const topic_id: number | undefined = msg?.message_thread_id ? Number(msg.message_thread_id) : undefined
	const is_command = typeof text === 'string' && text.trim().startsWith('/')
	return { chat_id, text, msg_id, reply_to_msg_id, topic_id, is_command }
}

export async function onTelegramUpdate(bot_id: string, update: Update): Promise<{ status: number, body: any }> {
	const ctx = toCtx(bot_id, update)
	if (!ctx.chat_id || (!ctx.text && !ctx.reply_to_msg_id)) return { status: 200, body: { ok: true } }

	// /start payloads: bind:<hub_id> or pair <code>
	try {
		const start = (ctx.text || '').trim()
		if (start.startsWith('/start ')) {
			const payload = start.slice('/start '.length); log.info({ chat_id: ctx.chat_id, payload }, 'tg: /start payload')
			if (payload.startsWith('bind:')) {
				const hub = payload.slice('bind:'.length)
				if (hub) {
					const alias = 'hub'
					await upsertBinding(ctx.chat_id, hub, alias, false)
					try { await sendToTelegram({ chat_id: ctx.chat_id, text: `Linked to ${hub} as ${alias}` }) } catch { }
					return { status: 200, body: { ok: true, routed: false } }
				}
			} else {
				const code = payload; log.info({ chat_id: ctx.chat_id, code }, 'tg: /start code')
				try {
					const rec = await pairConfirm(code); log.info({ chat_id: ctx.chat_id, state: rec?.state, hub_id: rec?.hub_id }, 'tg: pairConfirm result')
					const hubId = rec && rec.state === 'confirmed' ? (rec.hub_id || undefined) : undefined
					if (hubId) {
						try { await tgLinkSet(hubId, String(ctx.chat_id), bot_id, String(ctx.chat_id)) } catch { }
						try {
							const existing = await listBindings(ctx.chat_id)
							let alias = 'hub'
							const names = new Set((existing || []).map(b => String(b.alias)))
							if (names.has(alias)) { let i = 2; while (names.has(`hub-${i}`)) i++; alias = `hub-${i}` }
							const makeDefault = (existing || []).length === 0
							await upsertBinding(ctx.chat_id, hubId, alias, makeDefault)
							if (makeDefault) { try { await setSession(ctx.chat_id, hubId, 'manual') } catch { } }
						} catch { }
						try { await sendToTelegram({ chat_id: ctx.chat_id, text: 'Pair confirmed' }) } catch { }
						return { status: 200, body: { ok: true, routed: false } }
					}
				} catch (e) {
					log.warn({ err: String(e) }, 'pair confirm failed in router')
				}
			}
		}
	} catch { }

	// Commands
	if (ctx.is_command && ctx.text) {
		try {
			const res = await handleCommand({ chat_id: ctx.chat_id, text: ctx.text, topic_id: ctx.topic_id });
			if (res) {
				try { await sendToTelegram({ chat_id: ctx.chat_id, text: res.text, keyboard: res.keyboard }) } catch { }
				return { status: 200, body: { ok: true, routed: false } }
			}
		} catch (e) {
			log.warn({ chat_id: ctx.chat_id, err: String(e) }, 'tg: handleCommand failed');
			try { await sendToTelegram({ chat_id: ctx.chat_id, text: 'Command failed, try again later' }) } catch { }
			return { status: 200, body: { ok: true, routed: false } }
		}
	}

	// Resolve target by priorities
	try {
		const target = await resolveTarget({ chat_id: ctx.chat_id, text: ctx.text, reply_to_msg_id: ctx.reply_to_msg_id, topic_id: ctx.topic_id })
		const clean = stripExplicitAlias(ctx.text)
		const payload = { text: clean, chat_id: ctx.chat_id, tg_msg_id: ctx.msg_id, bot_id, route: { via: target.via, alias: target.alias, session_id: undefined }, meta: { is_command: false } }
		await publishIn(target.hub_id, payload)
		await logMessage(ctx.chat_id, ctx.msg_id, target.hub_id, target.alias, target.via)
		return { status: 200, body: { ok: true, routed: true } }
	} catch (e) {
		if (String(e).includes('need_choice')) {
			try {
				const binds = await listBindings(ctx.chat_id)
				const count = (binds || []).length
				if (count === 1 && ctx.text) {
					const b = binds![0]
					const clean = stripExplicitAlias(ctx.text)
					const payload = { text: clean, chat_id: ctx.chat_id, tg_msg_id: ctx.msg_id, bot_id, route: { via: 'default', alias: b.alias, session_id: undefined }, meta: { is_command: false } }
					await publishIn(b.hub_id as any, payload)
					try { await logMessage(ctx.chat_id, ctx.msg_id, b.hub_id as any, b.alias as any, 'default') } catch { }
					return { status: 200, body: { ok: true, routed: true } }
				}
				if (count <= 1) {
					try { await sendToTelegram({ chat_id: ctx.chat_id, text: 'No bindings yet. Use /start bind:<hub_id> or /start <code>' }) } catch { }
					try { await logMessage(ctx.chat_id, ctx.msg_id, null, null, 'none') } catch { }
					return { status: 200, body: { ok: true, routed: false } }
				}
			} catch { }
			try { await sendToTelegram({ chat_id: ctx.chat_id, text: 'Pick a subnet: use /list and /use <alias> or send @alias <text>' }) } catch { }
			try { await logMessage(ctx.chat_id, ctx.msg_id, null, null, 'none') } catch { }
			return { status: 200, body: { ok: true, routed: false } }
		}
		try { await logMessage(ctx.chat_id, ctx.msg_id, null, null, 'none') } catch { }
		return { status: 200, body: { ok: true, routed: false } }
	}
}
