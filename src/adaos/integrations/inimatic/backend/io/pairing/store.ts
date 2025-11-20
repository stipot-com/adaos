import { randomBytes } from 'crypto'
import { redis } from '../idem/kv.js'
import { tgLinkSetDb, tgLinkGetDb } from '../../db/tg.repo.js'

export type PairRecord = {
  bot_id: string
  hub_id?: string | null
  state: 'issued' | 'confirmed' | 'revoked' | 'expired'
  expires_at: number
}

function genCode(): string {
  // base32-like without confusing chars
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
  const bytes = randomBytes(8)
  let out = ''
  for (let i = 0; i < bytes.length; i++) out += alphabet[bytes[i] % alphabet.length]
  return out.slice(0, 10)
}

export async function pairCreate(bot_id: string, hub_id: string | undefined, ttlSec: number) {
  const code = genCode()
  const now = Math.floor(Date.now() / 1000)
  const rec: PairRecord = { bot_id, hub_id, state: 'issued', expires_at: now + ttlSec }
  await redis.set(`pair:${code}`, JSON.stringify(rec), 'EX', ttlSec)
  return { code, ...rec }
}

export async function pairGet(code: string): Promise<PairRecord | null> {
  const raw = await redis.get(`pair:${code}`)
  return raw ? (JSON.parse(raw) as PairRecord) : null
}

export async function pairConfirm(code: string): Promise<PairRecord | null> {
  const key = `pair:${code}`
  const raw = await redis.get(key)
  if (!raw) return null
  const rec = JSON.parse(raw) as PairRecord
  const now = Math.floor(Date.now() / 1000)
  if (rec.expires_at < now) {
    rec.state = 'expired'
  } else if (rec.state === 'issued') {
    rec.state = 'confirmed'
  }
  await redis.set(key, JSON.stringify(rec), 'EX', Math.max(1, rec.expires_at - now))
  return rec
}

export async function pairRevoke(code: string): Promise<boolean> {
  const key = `pair:${code}`
  const raw = await redis.get(key)
  if (!raw) return false
  const rec = JSON.parse(raw) as PairRecord
  rec.state = 'revoked'
  const now = Math.floor(Date.now() / 1000)
  await redis.set(key, JSON.stringify(rec), 'EX', Math.max(1, rec.expires_at - now))
  return true
}

// chat_bindings — minimal Redis-backed (replace with DB DAO later)
export type Binding = { platform: string; user_id: string; bot_id: string; ada_user_id: string; hub_id?: string | null; created_at: number; last_seen: number }

export async function bindingUpsert(platform: string, user_id: string, bot_id: string, hub_id?: string | null): Promise<Binding> {
  const key = `bind:${platform}:${user_id}:${bot_id}`
  const now = Math.floor(Date.now() / 1000)
  const old = await redis.get(key)
  const ada_user_id = old ? (JSON.parse(old) as Binding).ada_user_id : randomBytes(8).toString('hex')
  const rec: Binding = { platform, user_id, bot_id, ada_user_id, hub_id, created_at: now, last_seen: now }
  await redis.set(key, JSON.stringify(rec))
  return rec
}

export async function bindingGet(platform: string, user_id: string, bot_id: string): Promise<Binding | null> {
  const raw = await redis.get(`bind:${platform}:${user_id}:${bot_id}`)
  return raw ? (JSON.parse(raw) as Binding) : null
}

// Telegram hub↔chat link by hub_id to simplify outbound sending
export type TgLink = { hub_id: string; owner_id: string; bot_id: string; chat_id: string; updated_at: number }

export async function tgLinkSet(hub_id: string, owner_id: string, bot_id: string, chat_id: string): Promise<TgLink> {
  try {
    const rec = await tgLinkSetDb(hub_id, owner_id, bot_id, chat_id)
    return rec
  } catch {
    // fallback to redis if db unavailable
    const key = `tgpair:${hub_id}`
    const rec: TgLink = { hub_id, owner_id, bot_id, chat_id, updated_at: Math.floor(Date.now() / 1000) }
    await redis.set(key, JSON.stringify(rec))
    return rec
  }
}

export async function tgLinkGet(hub_id: string): Promise<TgLink | null> {
  try {
    const rec = await tgLinkGetDb(hub_id)
    if (rec) return rec
  } catch { /* ignore */ }
  const raw = await redis.get(`tgpair:${hub_id}`)
  return raw ? (JSON.parse(raw) as TgLink) : null
}
