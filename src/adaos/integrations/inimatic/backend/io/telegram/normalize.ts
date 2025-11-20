import type { ChatInputEvent } from '../types.js'

export function toInputEvent(
	bot_id: string,
	update: any,
	hub_id?: string | null
): ChatInputEvent {
	const upd_id = String(update?.update_id ?? '')

	const hasCb = !!update?.callback_query
	const cb = hasCb ? update.callback_query : undefined
	const msg = update?.message || update?.edited_message || undefined
	const baseMsg = hasCb ? (cb?.message ?? undefined) : msg

	// user_id
	const user_id =
		(baseMsg?.from?.id != null ? String(baseMsg.from.id) : undefined) ??
		(cb?.from?.id != null ? String(cb.from.id) : undefined) ??
		''

	// chat_id
	const chat_id =
		(baseMsg?.chat?.id != null ? String(baseMsg.chat.id) : undefined) ??
		(cb?.message?.chat?.id != null ? String(cb.message.chat.id) : undefined) ??
		''

	// язык (если есть)
	const lang =
		baseMsg?.from?.language_code ??
		cb?.from?.language_code

	// TEXT (/start code и т.п.)
	if (typeof baseMsg?.text === 'string' && baseMsg.text.length > 0) {
		return {
			type: 'text',
			source: 'telegram',
			bot_id,
			hub_id,
			chat_id,
			user_id,
			update_id: upd_id,
			payload: {
				text: baseMsg.text,
				meta: { msg_id: baseMsg.message_id, lang },
			},
		}
	}

	// CALLBACK DATA
	if (typeof cb?.data === 'string' && cb.data.length > 0) {
		return {
			type: 'action',
			source: 'telegram',
			bot_id,
			hub_id,
			chat_id,
			user_id,
			update_id: upd_id,
			payload: { action: { id: cb.data }, meta: { msg_id: cb.message?.message_id } },
		}
	}

	// VOICE
    if (baseMsg?.voice?.file_id) {
        const v = baseMsg.voice
        return {
            type: 'audio',
            source: 'telegram',
            bot_id,
            hub_id,
            chat_id,
            user_id,
            update_id: upd_id,
            payload: {
                file_id: v.file_id,
                // voice messages have no caption in Telegram API; keep meta only
                meta: { msg_id: baseMsg.message_id, mime: 'audio/ogg', duration: v.duration },
            },
        }
    }

	// PHOTO (берём последний размер)
    if (Array.isArray(baseMsg?.photo) && baseMsg.photo.length > 0) {
        const sizes = baseMsg.photo
        const file_id = sizes[sizes.length - 1]?.file_id
        const caption: string | undefined = typeof baseMsg.caption === 'string' && baseMsg.caption.length > 0 ? baseMsg.caption : undefined
        return {
            type: 'photo',
            source: 'telegram',
            bot_id,
            hub_id,
            chat_id,
            user_id,
            update_id: upd_id,
            payload: { file_id, ...(caption ? { text: caption } : {}), meta: { msg_id: baseMsg.message_id } },
        }
    }

	// DOCUMENT
    if (baseMsg?.document?.file_id) {
        const d = baseMsg.document
        const caption: string | undefined = typeof baseMsg.caption === 'string' && baseMsg.caption.length > 0 ? baseMsg.caption : undefined
        return {
            type: 'document',
            source: 'telegram',
            bot_id,
            hub_id,
            chat_id,
            user_id,
            update_id: upd_id,
            payload: { file_id: d.file_id, ...(caption ? { text: caption } : {}), meta: { msg_id: baseMsg.message_id } },
        }
    }

	// UNKNOWN
	return {
		type: 'unknown',
		source: 'telegram',
		bot_id,
		hub_id,
		chat_id,
		user_id,
		update_id: upd_id,
		payload: { meta: { raw_kind: 'unknown', lang } },
	}
}
