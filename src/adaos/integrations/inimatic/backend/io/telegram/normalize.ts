import { ChatInputEvent } from '../types.js'

export function toInputEvent(bot_id: string, update: any, hub_id?: string | null): ChatInputEvent {
  const upd_id = String(update?.update_id ?? '')
  const msg = update?.message || update?.edited_message || {}
  const cb = update?.callback_query || {}
  const baseMsg = cb ? (cb.message || {}) : msg
  const frm = (baseMsg?.from || cb?.from) || {}
  const chat = baseMsg?.chat || {}
  const user_id = String(frm?.id ?? '')
  const chat_id = String(chat?.id ?? (cb?.message?.chat?.id ?? ''))

  if (baseMsg?.text) {
    return { type: 'text', source: 'telegram', bot_id, hub_id, chat_id, user_id, update_id: upd_id, payload: { text: baseMsg.text, meta: { msg_id: baseMsg.message_id, lang: frm.language_code } } }
  }
  if (cb?.data) {
    return { type: 'action', source: 'telegram', bot_id, hub_id, chat_id, user_id, update_id: upd_id, payload: { action: { id: cb.data }, meta: { msg_id: cb.message?.message_id } } }
  }
  if (baseMsg?.voice) {
    const v = baseMsg.voice
    return { type: 'audio', source: 'telegram', bot_id, hub_id, chat_id, user_id, update_id: upd_id, payload: { file_id: v.file_id, meta: { msg_id: baseMsg.message_id, mime: 'audio/ogg', duration: v.duration } } }
  }
  if (baseMsg?.photo) {
    const sizes = baseMsg.photo
    const file_id = sizes?.length ? sizes[sizes.length - 1].file_id : undefined
    return { type: 'photo', source: 'telegram', bot_id, hub_id, chat_id, user_id, update_id: upd_id, payload: { file_id, meta: { msg_id: baseMsg.message_id } } }
  }
  if (baseMsg?.document) {
    const d = baseMsg.document
    return { type: 'document', source: 'telegram', bot_id, hub_id, chat_id, user_id, update_id: upd_id, payload: { file_id: d.file_id, meta: { msg_id: baseMsg.message_id } } }
  }
  return { type: 'unknown', source: 'telegram', bot_id, hub_id, chat_id, user_id, update_id: upd_id, payload: { meta: { raw_kind: 'unknown' } } }
}

