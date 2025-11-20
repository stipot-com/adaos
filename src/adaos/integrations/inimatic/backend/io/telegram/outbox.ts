type SendOpts = { reply_to_message_id?: number, keyboard?: any }

async function tgCall(method: string, payload: any): Promise<void> {
  const token = process.env['TG_BOT_TOKEN'] || ''
  if (!token) return
  const url = `https://api.telegram.org/bot${token}/${method}`
  const { request } = await import('undici')
  await request(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload) })
}

export async function sendToTelegram(params: { chat_id: number, text: string, alias?: string } & SendOpts): Promise<void> {
  const prefix = params.alias ? `[${params.alias}]: ` : ''
  const payload: any = { chat_id: params.chat_id, text: prefix + params.text }
  if (params.reply_to_message_id) payload.reply_to_message_id = params.reply_to_message_id
  if (params.keyboard) payload.reply_markup = params.keyboard
  await tgCall('sendMessage', payload)
}

