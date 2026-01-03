// src\adaos\integrations\inimatic\backend\io\pairing\api.ts
import express from 'express'
import pino from 'pino'
import { verifyWebSessionJwt } from '../../sessionJwt.js'
import {
	pairConfirm,
	pairCreate,
	pairGet,
	pairRevoke,
	bindingUpsert,
	tgLinkGet,
	tgLinkSet,
	browserPairApprove,
	browserPairConsume,
	browserPairCreate,
	browserPairGet,
	browserPairRevoke,
} from './store.js'

const log = pino({ name: 'pair-api' })

export function installPairingApi(app: express.Express) {
	function extractBearer(headerValue: string): string | undefined {
		const trimmed = String(headerValue || '').trim()
		const match = trimmed.match(/^Bearer\s+(.+)$/i)
		return match ? match[1].trim() : undefined
	}

	async function readWebSessionClaims(req: express.Request): Promise<any | null> {
		const token = extractBearer(req.header('Authorization') ?? '') || String(req.query['session_jwt'] || '').trim()
		if (!token) return null
		const secret = String(process.env['WEB_SESSION_JWT_SECRET'] || '').trim()
		if (!secret) return null
		const claims = await verifyWebSessionJwt({ secret, token })
		return claims
	}

	function buildPublicNatsWsUrl(): string {
		const baseHttp = (process.env['TG_WEBHOOK_BASE'] || 'https://api.inimatic.com').replace(/\/+$/, '')
		const baseUrl = new URL(baseHttp)
		const wsProto = baseUrl.protocol.startsWith('http') ? baseUrl.protocol.replace('http', 'ws') : 'wss:'
		baseUrl.protocol = wsProto
		const base = baseUrl.toString().replace(/\/+$/, '')

		const explicit = (process.env['NATS_WS_PUBLIC'] || '').trim()
		if (explicit) return explicit

		const rawPath = (process.env['WS_NATS_PATH'] || '/nats').trim() || '/nats'
		const wsPath = rawPath.startsWith('/') ? rawPath : `/${rawPath}`
		return wsPath === '/' ? base : `${base}${wsPath}`
	}

	app.post('/io/tg/pair/create', async (req, res) => {
		try {
			const ttl = Number.parseInt(String((req.query['ttl'] as string) ?? (req.body?.ttl as string) ?? '600'), 10) || 600
			const hub = typeof req.query['hub'] === 'string'
				? (req.query['hub'] as string)
				: (typeof req.body?.hub_id === 'string' ? (req.body.hub_id as string) : (typeof req.body?.hub === 'string' ? (req.body.hub as string) : undefined))
			const bot = typeof req.query['bot'] === 'string'
				? (req.query['bot'] as string)
				: (typeof req.body?.bot_id === 'string' ? (req.body.bot_id as string) : (process.env['BOT_ID'] || 'main-bot'))
			log.info({ tag: 'PAIR', route: 'create', hub, bot, ttl }, '[PAIR] create: request')
			const rec = await pairCreate(bot, hub, ttl)
			const deep_link = process.env['BOT_USERNAME'] ? `https://t.me/${process.env['BOT_USERNAME']}?start=${rec.code}` : undefined
			log.info({ tag: 'PAIR', route: 'create', hub: rec.hub_id, code: rec.code, expires_at: rec.expires_at }, '[PAIR] create: issued')
			const ws_url = buildPublicNatsWsUrl()
			const nats_user = rec.hub_id ? `hub_${rec.hub_id}` : undefined
			res.json({ ok: true, pair_code: rec.code, deep_link, expires_at: rec.expires_at, hub_id: rec.hub_id, nats_ws_url: ws_url, nats_user })
		} catch (e) {
			log.error({ tag: 'PAIR', route: 'create', err: String(e) }, '[PAIR] create: error')
			res.status(500).json({ ok: false })
		}
	})

	// alias under /v1 for clients using the old path (support body and query)
	app.post('/v1/pair/create', async (req, res) => {
		try {
			const ttl = Number.parseInt(String((req.query['ttl'] as string) ?? (req.body?.ttl as string) ?? '600'), 10) || 600
			const hub = typeof req.query['hub'] === 'string'
				? (req.query['hub'] as string)
				: (typeof req.body?.hub_id === 'string' ? (req.body.hub_id as string) : (typeof req.body?.hub === 'string' ? (req.body.hub as string) : undefined))
			const bot = typeof req.query['bot'] === 'string'
				? (req.query['bot'] as string)
				: (typeof req.body?.bot_id === 'string' ? (req.body.bot_id as string) : (process.env['BOT_ID'] || 'main-bot'))
			log.info({ tag: 'PAIR', route: 'create.v1', hub, bot, ttl }, '[PAIR] v1/create: request')
			const rec = await pairCreate(bot, hub, ttl)
			log.info({ tag: 'PAIR', route: 'create.v1', hub: rec.hub_id, code: rec.code, expires_at: rec.expires_at }, '[PAIR] v1/create: issued')
			const ws_url = buildPublicNatsWsUrl()
			const nats_user = rec.hub_id ? `hub_${rec.hub_id}` : undefined
			res.json({ ok: true, pair_code: rec.code, expires_at: rec.expires_at, hub_id: rec.hub_id, nats_ws_url: ws_url, nats_user })
		} catch (e) {
			log.error({ tag: 'PAIR', route: 'create.v1', err: String(e) }, '[PAIR] v1/create: error')
			res.status(500).json({ ok: false })
		}
	})

	// ------------------------------------------------------------
	// Browser QR pairing (web)
	// ------------------------------------------------------------

	app.post('/v1/browser/pair/create', async (req, res) => {
		try {
			const ttl = Number.parseInt(String((req.query['ttl'] as string) ?? (req.body?.ttl as string) ?? '600'), 10) || 600
			const rec = await browserPairCreate(ttl)
			log.info({ tag: 'BPAIR', route: 'create', code: rec.code, expires_at: rec.expires_at }, '[BPAIR] create')
			res.json({ ok: true, pair_code: rec.code, expires_at: rec.expires_at })
		} catch (e) {
			log.error({ tag: 'BPAIR', route: 'create', err: String(e) }, '[BPAIR] create: error')
			res.status(500).json({ ok: false })
		}
	})

	app.get('/v1/browser/pair/status', async (req, res) => {
		const code = String(req.query['code'] || req.query['pair_code'] || '')
		if (!code) return res.status(400).json({ ok: false, error: 'code_required' })
		const rec = await browserPairGet(code)
		if (!rec) return res.json({ ok: true, state: 'not_found' })
		const now = Math.floor(Date.now() / 1000)
		if (rec.expires_at < now && rec.state !== 'expired') {
			rec.state = 'expired'
		}
		const expires_in = Math.max(0, rec.expires_at - now)
		const payload: any = { ok: true, state: rec.state, expires_in }
		if (rec.state === 'approved') {
			// Warning: for MVP/dev we return the approving web session JWT to bootstrap the device.
			payload.session_jwt = rec.session_jwt || null
			payload.hub_id = rec.hub_id || null
			payload.webspace_id = rec.webspace_id || null
		}
		res.json(payload)
	})

	app.post('/v1/browser/pair/approve', async (req, res) => {
		const code = String(req.body?.code || req.query['code'] || req.body?.pair_code || req.query['pair_code'] || '')
		if (!code) return res.status(400).json({ ok: false, error: 'code_required' })
		const secret = String(process.env['WEB_SESSION_JWT_SECRET'] || '').trim()
		if (!secret) {
			log.error({ tag: 'BPAIR', route: 'approve', code }, '[BPAIR] approve: missing WEB_SESSION_JWT_SECRET')
			return res.status(500).json({ ok: false, error: 'server_misconfig' })
		}
		const claims = await readWebSessionClaims(req)
		// Web session JWT currently only guarantees owner_id; hub_id/subnet_id may be absent.
		// For MVP, treat owner_id/subnet_id as hub_id when needed.
		const hub_id = String(claims?.hub_id || claims?.subnet_id || claims?.owner_id || '').trim()
		const owner_id = String(claims?.owner_id || claims?.subnet_id || claims?.hub_id || '').trim()
		if (!hub_id || !owner_id) {
			const keys = claims && typeof claims === 'object' ? Object.keys(claims as any) : []
			log.warn(
				{ tag: 'BPAIR', route: 'approve', code, hasClaims: Boolean(claims), claimKeys: keys, hub_id, owner_id },
				'[BPAIR] approve: unauthorized'
			)
			return res.status(401).json({ ok: false, error: 'unauthorized' })
		}

		const token = extractBearer(req.header('Authorization') ?? '') || String(req.query['session_jwt'] || '').trim()
		if (!token) {
			log.warn({ tag: 'BPAIR', route: 'approve', code, hub_id }, '[BPAIR] approve: missing token')
			return res.status(401).json({ ok: false, error: 'missing_token' })
		}
		const webspace_id = typeof req.body?.webspace_id === 'string' ? req.body.webspace_id : (typeof req.query['webspace_id'] === 'string' ? (req.query['webspace_id'] as string) : undefined)
		const rec = await browserPairApprove({ code, hub_id, session_jwt: token, webspace_id: webspace_id ?? null })
		if (!rec) return res.status(404).json({ ok: false, error: 'not_found' })
		if (rec.state === 'expired') return res.status(400).json({ ok: false, error: 'expired' })
		if (rec.state === 'revoked') return res.status(400).json({ ok: false, error: 'revoked' })
		log.info({ tag: 'BPAIR', route: 'approve', code, hub_id, owner_id, webspace_id: webspace_id ?? null }, '[BPAIR] approve')
		res.json({ ok: true, state: rec.state, expires_at: rec.expires_at })
	})

	app.post('/v1/browser/pair/consume', async (req, res) => {
		const code = String(req.body?.code || req.query['code'] || req.body?.pair_code || req.query['pair_code'] || '')
		if (!code) return res.status(400).json({ ok: false, error: 'code_required' })
		const rec = await browserPairConsume(code)
		if (!rec) return res.status(404).json({ ok: false, error: 'not_found' })
		res.json({ ok: true, state: rec.state })
	})

	app.post('/v1/browser/pair/revoke', async (req, res) => {
		const code = String(req.body?.code || req.query['code'] || req.body?.pair_code || req.query['pair_code'] || '')
		if (!code) return res.status(400).json({ ok: false, error: 'code_required' })
		const ok = await browserPairRevoke(code)
		res.json({ ok })
	})

	app.post('/io/tg/pair/confirm', async (req, res) => {
		const code = String(req.body?.code || req.query['code'] || '')
		if (!code) return res.status(400).json({ error: 'code_required' })
		log.info({ tag: 'PAIR', route: 'confirm', code }, '[PAIR] confirm: request')
		const rec = await pairConfirm(code)
		if (!rec) return res.status(404).json({ ok: false, error: 'not_found' })
		if (rec.state === 'expired') return res.status(400).json({ ok: false, error: 'expired' })
		if (rec.state === 'revoked') return res.status(400).json({ ok: false, error: 'revoked' })
		// allow treating user_id as hub_id as per MVP
		const user_id = String(req.body?.user_id || req.query['user_id'] || rec.hub_id || '')
		const bot_id = String(req.body?.bot_id || req.query['bot_id'] || rec.bot_id || '')
		const binding = await bindingUpsert('telegram', user_id, bot_id, rec.hub_id)
		// optional: if chat_id is provided explicitly, persist hub→chat link now
		const chat_id = (req.body?.chat_id || req.query['chat_id']) as string | undefined
		if (chat_id && rec.hub_id) {
			try {
				await tgLinkSet(rec.hub_id, user_id || rec.hub_id!, bot_id, String(chat_id))
				log.info({ tag: 'PAIR', route: 'confirm', hub_id: rec.hub_id, chat_id: String(chat_id), bot_id }, '[PAIR] confirm: tgLinkSet (explicit chat_id)')
			} catch (e) { log.warn({ tag: 'PAIR', route: 'confirm', err: String(e) }, '[PAIR] confirm: tgLinkSet failed') }
		}
		log.info({ tag: 'PAIR', route: 'confirm', hub_id: binding.hub_id, ada_user_id: binding.ada_user_id }, '[PAIR] confirm: done')
		res.json({ ok: true, hub_id: binding.hub_id, ada_user_id: binding.ada_user_id })
	})

	// alias under /v1 for clients using the old path
	app.post('/v1/pair/confirm', async (req, res) => {
		const code = String(req.body?.code || req.query['code'] || '')
		if (!code) return res.status(400).json({ error: 'code_required' })
		const rec = await pairConfirm(code)
		if (!rec) return res.status(404).json({ ok: false, error: 'not_found' })
		if (rec.state === 'expired') return res.status(400).json({ ok: false, error: 'expired' })
		if (rec.state === 'revoked') return res.status(400).json({ ok: false, error: 'revoked' })
		const user_id = String(req.body?.user_id || req.query['user_id'] || rec.hub_id || '')
		const bot_id = String(req.body?.bot_id || req.query['bot_id'] || rec.bot_id || '')
		const binding = await bindingUpsert('telegram', user_id, bot_id, rec.hub_id)
		const chat_id = (req.body?.chat_id || req.query['chat_id']) as string | undefined
		if (chat_id && rec.hub_id) {
			try { await tgLinkSet(rec.hub_id, user_id || rec.hub_id!, bot_id, String(chat_id)) } catch {}
		}
		res.json({ ok: true, hub_id: binding.hub_id, ada_user_id: binding.ada_user_id })
	})

	app.get('/io/tg/pair/status', async (req, res) => {
		const code = String(req.query['code'] || '')
		if (!code) return res.status(400).json({ error: 'code_required' })
		log.info({ tag: 'PAIR', route: 'status', code }, '[PAIR] status: request')
		const rec = await pairGet(code)
		if (!rec) return res.json({ ok: true, state: 'not_found' })
		const ttl = Math.max(0, rec.expires_at - Math.floor(Date.now() / 1000))
		log.info({ tag: 'PAIR', route: 'status', state: rec.state, expires_in: ttl, hub_id: rec.hub_id }, '[PAIR] status: found')
		res.json({ ok: true, state: rec.state, expires_in: ttl, hub_id: rec.hub_id })
	})

	app.post('/io/tg/pair/revoke', async (req, res) => {
		const code = String(req.body?.code || req.query['code'] || '')
		if (!code) return res.status(400).json({ error: 'code_required' })
		log.info({ tag: 'PAIR', route: 'revoke', code }, '[PAIR] revoke: request')
		const ok = await pairRevoke(code)
		res.json({ ok })
	})

	// Debug/diagnostic: check hub→telegram link presence
	app.get('/io/tg/pair/link', async (req, res) => {
		const hub_id = String(req.query['hub_id'] || '')
		if (!hub_id) return res.status(400).json({ error: 'hub_id_required' })
		log.info({ tag: 'PAIR', route: 'link', hub_id }, '[PAIR] link: request')
		const link = await tgLinkGet(hub_id)
		if (!link) return res.status(404).json({ ok: false, error: 'pairing_not_found', hub_id })
		log.info({ tag: 'PAIR', route: 'link', hub_id, chat_id: link.chat_id, bot_id: link.bot_id }, '[PAIR] link: found')
		return res.json({ ok: true, link })
	})
}
