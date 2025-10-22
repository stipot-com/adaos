export type ChatInputEvent = {
  type: 'text' | 'audio' | 'photo' | 'document' | 'action' | 'unknown'
  source: 'telegram'
  bot_id: string
  hub_id?: string | null
  chat_id: string
  user_id: string
  update_id: string
  payload: Record<string, any>
}

export type ChatOutputMessage = {
  type: 'text' | 'voice' | 'photo'
  text?: string
  audio_path?: string
  image_path?: string
  keyboard?: Record<string, any>
}

export type ChatOutputEvent = {
  target: { bot_id: string; hub_id: string; chat_id: string }
  messages: ChatOutputMessage[]
  options?: { replace_last?: boolean; reply_to?: number }
}

