// backend/io/telegram/sender.ts
import { fetch, FormData, File, type Response as UndiciResponse } from 'undici'
import fs from 'node:fs'
import path from 'node:path'
import pino from 'pino'
import type { ChatOutputEvent, ChatOutputMessage } from '../types.js'
import { redis } from '../idem/kv.js'
import { outbound_total, retry_total } from '../telemetry.js'

const log = pino({ name: 'telegram-sender' })

class TokenBucket {
	private tokens: number
	private updated = Date.now()
	constructor(private ratePerSec: number, private capacity: number) {
		this.tokens = capacity
	}
	async allow(cost = 1): Promise<boolean> {
		const now = Date.now()
		const elapsed = (now - this.updated) / 1000
		this.updated = now
		this.tokens = Math.min(this.capacity, this.tokens + elapsed * this.ratePerSec)
		if (this.tokens >= cost) { this.tokens -= cost; return true }
		return false
	}
}

const buckets = new Map<string, TokenBucket>()
function limiter(chat_id: string): TokenBucket {
	let b = buckets.get(chat_id)
	if (!b) { b = new TokenBucket(1.0, 30); buckets.set(chat_id, b) }
	return b
}

function dedupeTtlSec(): number {
	const raw = Number.parseInt(String(process.env['TG_OUTBOUND_DEDUPE_TTL_S'] || '600'), 10)
	return Number.isFinite(raw) && raw > 0 ? raw : 600
}

function operationKey(out: ChatOutputEvent): string {
	const protocol = (out as any)?._protocol
	const explicit =
		protocol && typeof protocol === 'object' && typeof protocol['operation_key'] === 'string'
			? String(protocol['operation_key']).trim()
			: ''
	if (explicit) return explicit
	const payload = JSON.stringify({
		target: out.target,
		messages: out.messages,
		options: out.options ?? null,
	})
	return `fallback:${out.target.chat_id}:${hashText(payload)}`
}

function fileFromPathSync(filePath: string, mime = 'application/octet-stream'): File {
	const data = fs.readFileSync(filePath)
	return new File([data], path.basename(filePath), { type: mime })
}

async function withRetries(fn: () => Promise<UndiciResponse>, attempts = 3): Promise<void> {
	let backoff = 500
	for (let i = 0; i < attempts; i++) {
		try {
			const res = await fn()
			if ([200, 201, 202].includes(res.status)) return
			if ([429, 500, 502, 503, 504].includes(res.status)) {
				retry_total.inc({ stage: 'outbound' })
				await new Promise(r => setTimeout(r, backoff))
				backoff = Math.min(backoff * 2, 5000)
				continue
			}
			log.warn({ status: res.status }, 'telegram http non-ok')
			return
		} catch (e) {
			retry_total.inc({ stage: 'outbound' })
			await new Promise(r => setTimeout(r, backoff))
			backoff = Math.min(backoff * 2, 5000)
		}
	}
	throw new Error('telegram_http_failed')
}

export class TelegramSender {
	constructor(private botToken: string) { }

	async send(out: ChatOutputEvent): Promise<void> {
		const chat_id = out.target.chat_id
		const opKey = operationKey(out)
		for (const [index, m] of out.messages.entries()) {
			const redisKey = `tg:outbound:op:${opKey}:${index}`
			let reserved = false
			try {
				const accepted = await redis.set(
					redisKey,
					JSON.stringify({ chat_id, ts: Date.now(), index }),
					'EX',
					dedupeTtlSec(),
					'NX',
				)
				if (accepted !== 'OK') {
					log.info({ chat_id, operation_key: opKey, index }, 'telegram duplicate suppressed')
					continue
				}
				reserved = true
				await this.sendOne(out, m)
			} catch (error) {
				if (reserved) {
					try { await redis.del(redisKey) } catch { }
				}
				throw error
			}
		}
	}

	private async sendOne(out: ChatOutputEvent, m: ChatOutputMessage) {
		const chat_id = out.target.chat_id
		const base = `https://api.telegram.org/bot${this.botToken}`

		// rate limit per chat
		if (!(await limiter(chat_id).allow())) {
			await new Promise(r => setTimeout(r, 500))
		}

		if (m.type === 'text' && m.text) {
			await withRetries(() =>
				fetch(`${base}/sendMessage`, {
					method: 'POST',
					headers: { 'content-type': 'application/json' },
					body: JSON.stringify({ chat_id, text: m.text }),
				})
			)
			outbound_total.inc({ type: 'text' })

		} else if (m.type === 'photo' && m.image_path) {
			const fd = new FormData()
			fd.set('chat_id', String(chat_id))
			fd.set('photo', fileFromPathSync(m.image_path, 'image/jpeg'))
			await withRetries(() =>
				fetch(`${base}/sendPhoto`, { method: 'POST', body: fd as any })
			)
			outbound_total.inc({ type: 'photo' })

		} else if (m.type === 'voice' && m.audio_path) {
			const fd = new FormData()
			fd.set('chat_id', String(chat_id))
			fd.set('voice', fileFromPathSync(m.audio_path, 'audio/ogg'))
			await withRetries(() =>
				fetch(`${base}/sendVoice`, { method: 'POST', body: fd as any })
			)
			outbound_total.inc({ type: 'voice' })
		}
	}
}

function hashText(s: string): string {
	let h = 0
	for (let i = 0; i < s.length; i++) h = ((h << 5) - h) + s.charCodeAt(i), h |= 0
	return String(h >>> 0)
}
