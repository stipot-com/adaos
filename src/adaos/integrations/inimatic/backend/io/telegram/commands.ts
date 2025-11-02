import { listBindings, getDefaultBinding, setDefault, getByAlias, renameAlias, unlinkAlias, upsertBinding, setSession, bindTopic, unbindTopic } from '../../db/tg.repo.js'
import pino from 'pino'
import type { InlineKeyboardButton } from './keyboards.js'
import { keyboardPicker } from './keyboards.js'

export type CmdCtx = { chat_id: number, text: string, topic_id?: number }

const log = pino({ name: 'tg-commands' })

const HELP = [
  'Р”РѕСЃС‚СѓРїРЅС‹Рµ РєРѕРјР°РЅРґС‹:',
  '/use <alias|hub> вЂ” СЃРґРµР»Р°С‚СЊ С‚РµРєСѓС‰РµР№',
  '/current вЂ” РїРѕРєР°Р·Р°С‚СЊ С‚РµРєСѓС‰СѓСЋ Рё РґРµС„РѕР»С‚РЅСѓСЋ',
  '/list вЂ” СЃРїРёСЃРѕРє, РІС‹Р±СЂР°С‚СЊ С‚РµРєСѓС‰СѓСЋ/РґРµС„РѕР»С‚РЅСѓСЋ',
  '/default <alias> вЂ” СЃРґРµР»Р°С‚СЊ РґРµС„РѕР»С‚РЅРѕР№',
  '/alias <hub|alias> <new> вЂ” РїРµСЂРµРёРјРµРЅРѕРІР°С‚СЊ',
  '/unlink <alias> вЂ” РѕС‚РІСЏР·Р°С‚СЊ',
  '/bind_here <alias> вЂ” РїСЂРёРІСЏР·Р°С‚СЊ С‚РµРјСѓ Рє РїРѕРґСЃРµС‚Рё',
  '/unbind_here вЂ” СЃРЅСЏС‚СЊ РїСЂРёРІСЏР·РєСѓ С‚РµРјС‹',
  'РЇРІРЅР°СЏ Р°РґСЂРµСЃР°С†РёСЏ: @alias С‚РµРєСЃС‚',
].join('\n')

export async function handleCommand(ctx: CmdCtx): Promise<{ text: string, keyboard?: { inline_keyboard: InlineKeyboardButton[][] } } | null> {
  const parts = ctx.text.trim().split(/\s+/)
  const cmd = parts[0].toLowerCase()

  if (cmd === '/help') return { text: HELP }

  if (cmd === '/current') {
    log.info({ chat_id: ctx.chat_id, cmd }, 'tg: /current')
    const list = await listBindings(ctx.chat_id)
    const def = list.find(b => b.is_default)
    const current = def // for MVP, show default as current unless session logic added here
    const line = list.map(b => `${b.is_default ? 'в­ђ' : ' '} ${current && current.hub_id===b.hub_id ? 'вњ…' : ' '} ${b.alias} в†’ ${b.hub_id}`).join('\n') || 'РџСѓСЃС‚Рѕ'
    return { text: `РўРµРєСѓС‰Р°СЏ/РґРµС„РѕР»С‚РЅР°СЏ:\n${line}` }
  }

  if (cmd === '/list') {
    log.info({ chat_id: ctx.chat_id, cmd }, 'tg: /list')
    const list = await listBindings(ctx.chat_id)
    log.info({ chat_id: ctx.chat_id, count: list.length, items: list }, 'tg: /list result')
    const kb = keyboardPicker(list.map(b => ({ alias: b.alias, is_default: b.is_default })))
    return { text: 'РџРѕРґСЃРµС‚Рё:', keyboard: kb }
  }

  if (cmd === '/use' && parts[1]) {
    const key = parts[1]
    log.info({ chat_id: ctx.chat_id, cmd, key }, 'tg: /use')
    const b = (await getByAlias(ctx.chat_id, key))
    if (!b) return { text: 'РќРµ РЅР°Р№РґРµРЅ alias' }
    await setSession(ctx.chat_id, b.hub_id, 'manual')
    return { text: `РўРµРєСѓС‰Р°СЏ РїРѕРґСЃРµС‚СЊ: ${b.alias}` }
  }

  if (cmd === '/default' && parts[1]) {
    const alias = parts[1]
    log.info({ chat_id: ctx.chat_id, cmd, alias }, 'tg: /default')
    const b = await getByAlias(ctx.chat_id, alias)
    if (!b) return { text: 'РќРµ РЅР°Р№РґРµРЅ alias' }
    await setDefault(ctx.chat_id, alias)
    return { text: `Р”РµС„РѕР»С‚РЅР°СЏ: ${alias}` }
  }

  if (cmd === '/alias' && parts[1] && parts[2]) {
    const key = parts[1]
    const next = parts[2]
    log.info({ chat_id: ctx.chat_id, cmd, key, next }, 'tg: /alias')
    const ok = await renameAlias(ctx.chat_id, key, next)
    return { text: ok ? `РџРµСЂРµРёРјРµРЅРѕРІР°РЅРѕ: ${key} в†’ ${next}` : 'РќРµ РЅР°Р№РґРµРЅРѕ' }
  }

  if (cmd === '/unlink' && parts[1]) {
    const alias = parts[1]
    log.info({ chat_id: ctx.chat_id, cmd, alias }, 'tg: /unlink')
    const ok = await unlinkAlias(ctx.chat_id, alias)
    return { text: ok ? `РћС‚РІСЏР·Р°РЅРѕ: ${alias}` : 'РќРµ РЅР°Р№РґРµРЅРѕ' }
  }

  if (cmd === '/bind_here' && parts[1]) {
    if (!ctx.topic_id) return { text: 'РљРѕРјР°РЅРґР° РґРѕСЃС‚СѓРїРЅР° С‚РѕР»СЊРєРѕ РІ С‚РµРјР°С…' }
    const alias = parts[1]
    log.info({ chat_id: ctx.chat_id, cmd, alias, topic_id: ctx.topic_id }, 'tg: /bind_here')
    const b = await getByAlias(ctx.chat_id, alias)
    if (!b) return { text: 'РќРµ РЅР°Р№РґРµРЅ alias' }
    await bindTopic(ctx.chat_id, ctx.topic_id, b.hub_id)
    return { text: `РўРµРјР° РїСЂРёРІСЏР·Р°РЅР° Рє ${alias}` }
  }
  if (cmd === '/unbind_here') {
    if (!ctx.topic_id) return { text: 'РљРѕРјР°РЅРґР° РґРѕСЃС‚СѓРїРЅР° С‚РѕР»СЊРєРѕ РІ С‚РµРјР°С…' }
    log.info({ chat_id: ctx.chat_id, cmd, topic_id: ctx.topic_id }, 'tg: /unbind_here')
    await unbindTopic(ctx.chat_id, ctx.topic_id)
    return { text: 'РџСЂРёРІСЏР·РєР° С‚РµРјС‹ СЃРЅСЏС‚Р°' }
  }

  return null
}
