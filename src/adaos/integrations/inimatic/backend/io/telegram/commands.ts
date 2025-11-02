import { listBindings, getDefaultBinding, setDefault, getByAlias, renameAlias, unlinkAlias, upsertBinding, setSession, bindTopic, unbindTopic } from '../../db/tg.repo.js'
import pino from 'pino'
import type { InlineKeyboardButton } from './keyboards.js'
import { keyboardPicker } from './keyboards.js'

export type CmdCtx = { chat_id: number, text: string, topic_id?: number }

const log = pino({ name: 'tg-commands' })

const HELP = [
  'Commands:',
  '/start bind:<hub_id> - link a subnet to this chat',
  '/start <code> - confirm pairing code and finish linking',
  '/use <alias|hub> - set current',
  '/current - show current and default',
  '/list - list and pick current/default',
  '/default <alias> - set default',
  '/alias <hub|alias> <new> - rename alias',
  '/unlink <alias> - unlink alias',
  '/bind_here <alias> - in topics: bind this thread to a subnet',
  '/unbind_here - unbind current topic',
  'Explicit addressing: @alias text',
].join('\n')

export async function handleCommand(ctx: CmdCtx): Promise<{ text: string, keyboard?: { inline_keyboard: InlineKeyboardButton[][] } } | null> {
  const parts = ctx.text.trim().split(/\s+/)
  const cmd = parts[0].toLowerCase()

  if (cmd === '/help') return { text: HELP }

  if (cmd === '/current') {
    log.info({ chat_id: ctx.chat_id, cmd }, 'tg: /current')
    const list = await listBindings(ctx.chat_id)
    const def = list.find(b => b.is_default)
    const current = def
    const line = list.map(b => `${b.is_default ? '*' : ' '} ${current && current?.hub_id===b.hub_id ? '[x]' : '[ ]'} ${b.alias} -> ${b.hub_id}`).join('\n') || 'Empty'
    return { text: `Current/Default:\n${line}` }
  }

  if (cmd === '/list') {
    log.info({ chat_id: ctx.chat_id, cmd }, 'tg: /list')
    const list = await listBindings(ctx.chat_id)
    log.info({ chat_id: ctx.chat_id, count: list.length, items: list }, 'tg: /list result')
    const kb = keyboardPicker(list.map(b => ({ alias: b.alias, is_default: b.is_default })))
    return { text: 'Subnets:', keyboard: kb }
  }

  if (cmd === '/use' && parts[1]) {
    const key = parts[1]
    log.info({ chat_id: ctx.chat_id, cmd, key }, 'tg: /use')
    const b = (await getByAlias(ctx.chat_id, key))
    if (!b) return { text: 'Alias not found' }
    await setSession(ctx.chat_id, b.hub_id, 'manual')
    return { text: `Current subnet: ${b.alias}` }
  }

  if (cmd === '/default' && parts[1]) {
    const alias = parts[1]
    log.info({ chat_id: ctx.chat_id, cmd, alias }, 'tg: /default')
    const b = await getByAlias(ctx.chat_id, alias)
    if (!b) return { text: 'Alias not found' }
    await setDefault(ctx.chat_id, alias)
    return { text: `Default set: ${alias}` }
  }

  if (cmd === '/alias' && parts[1] && parts[2]) {
    const key = parts[1]
    const next = parts[2]
    log.info({ chat_id: ctx.chat_id, cmd, key, next }, 'tg: /alias')
    const ok = await renameAlias(ctx.chat_id, key, next)
    return { text: ok ? `Renamed: ${key} -> ${next}` : 'Not found' }
  }

  if (cmd === '/unlink' && parts[1]) {
    const alias = parts[1]
    log.info({ chat_id: ctx.chat_id, cmd, alias }, 'tg: /unlink')
    const ok = await unlinkAlias(ctx.chat_id, alias)
    return { text: ok ? `Unlinked: ${alias}` : 'Not found' }
  }

  if (cmd === '/bind_here' && parts[1]) {
    if (!ctx.topic_id) return { text: 'Command works only in topics' }
    const alias = parts[1]
    log.info({ chat_id: ctx.chat_id, cmd, alias, topic_id: ctx.topic_id }, 'tg: /bind_here')
    const b = await getByAlias(ctx.chat_id, alias)
    if (!b) return { text: 'Alias not found' }
    await bindTopic(ctx.chat_id, ctx.topic_id, b.hub_id)
    return { text: `Topic bound to ${alias}` }
  }
  if (cmd === '/unbind_here') {
    if (!ctx.topic_id) return { text: 'Command works only in topics' }
    log.info({ chat_id: ctx.chat_id, cmd, topic_id: ctx.topic_id }, 'tg: /unbind_here')
    await unbindTopic(ctx.chat_id, ctx.topic_id)
    return { text: 'Topic unbound' }
  }

  return null
}

