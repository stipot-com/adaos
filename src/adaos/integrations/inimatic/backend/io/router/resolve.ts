import { readFileSync } from 'node:fs'
import yaml from 'yaml'
import { bindingGet } from '../pairing/store.js'

type Rules = { default_hub?: string; locales?: Record<string, string> }
const cache = new Map<string, { ts: number; hub?: string | null }>()
const CACHE_TTL = 300 * 1000

function loadRules(): Rules | null {
  const p = process.env['ROUTE_RULES_PATH']
  if (!p) return null
  try {
    const data = yaml.parse(readFileSync(p, 'utf8')) as any
    return data || null
  } catch {
    return null
  }
}

export async function resolveHubId(platform: string, user_id: string, bot_id: string, locale?: string | null): Promise<string | undefined> {
  const key = `${platform}:${user_id}:${bot_id}`
  const now = Date.now()
  const c = cache.get(key)
  if (c && now - c.ts < CACHE_TTL) return c.hub || undefined

  const b = await bindingGet(platform, user_id, bot_id)
  if (b?.hub_id) {
    cache.set(key, { ts: now, hub: b.hub_id || undefined })
    return b.hub_id || undefined
  }

  const rules = loadRules()
  let hub: string | undefined
  if (rules?.locales && locale) {
    hub = rules.locales[locale] || rules.locales[locale.split('-')[0]]
  }
  if (!hub) hub = rules?.default_hub || process.env['DEFAULT_HUB'] || undefined
  cache.set(key, { ts: now, hub })
  return hub
}

