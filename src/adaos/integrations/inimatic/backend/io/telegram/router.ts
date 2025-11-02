import { handleCommand } from './commands.js'
import { ensureSchema, logMessage } from '../../db/tg.repo.js'
import { publishIn, subscribeOut } from '../../bus/nats.js'
import { sendToTelegram } from './outbox.js'
import { log } from '../../logging/routing.js'
import { toInputEvent } from './normalize.js'
import { resolveHubId } from '../router/resolve.js'
import { randomUUID } from 'crypto'

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
    } catch (e) { log.warn({ err: String(e) }, 'outbox deliver failed') }
  })
  _inited = true
}

export async function onTelegramUpdate(bot_id: string, update: Update): Promise<{ status: number, body: any }> {
  const text: string | undefined = update?.message?.text || update?.edited_message?.text || update?.callback_query?.data
  const is_command = typeof text === 'string' && text.trim().startsWith('/')
  const chat_id = Number(update?.message?.chat?.id || update?.edited_message?.chat?.id || update?.callback_query?.message?.chat?.id || 0)
  if (!chat_id) return { status: 200, body: { ok: true } }

  // Commands first
  if (is_command && text) {
    try {
      const res = await handleCommand({ chat_id, text, topic_id: Number(update?.message?.message_thread_id || 0) || undefined })
      if (res) { try { await sendToTelegram({ chat_id, text: res.text, keyboard: res.keyboard }) } catch {} ; return { status: 200, body: { ok: true, routed: false } } }
    } catch (e) { log.warn({ chat_id, err: String(e) }, 'tg: handleCommand failed'); try { await sendToTelegram({ chat_id, text: 'Command failed, try again later' }) } catch {} ; return { status: 200, body: { ok: true, routed: false } } }
  }

  // Normalize update → evt and publish to hub
  const evt = toInputEvent(bot_id, update, null)
  const locale = (evt.payload as any)?.meta?.lang
  const hub = (await resolveHubId('telegram', evt.user_id, bot_id, locale)) || process.env['DEFAULT_HUB']
  if (!hub) return { status: 200, body: { ok: true, routed: false } }

  const envelope = {
    event_id: randomUUID().replace(/-/g, ''),
    kind: 'io.input',
    ts: new Date().toISOString(),
    dedup_key: `${bot_id}:${evt.update_id}`,
    payload: evt,
    meta: { bot_id, hub_id: hub, trace_id: randomUUID().replace(/-/g, ''), retries: 0 },
  }
  await publishIn(hub, envelope)
  try { await logMessage(chat_id, Number(evt.update_id) || 0, hub, null, 'text') } catch {}
  return { status: 200, body: { ok: true, routed: true } }
}
