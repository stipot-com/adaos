// src\adaos\integrations\inimatic\backend\io\pairing\api.ts
import express from 'express'
import pino from 'pino'
import { pairConfirm, pairCreate, pairGet, pairRevoke, bindingUpsert, tgLinkGet, tgLinkSet } from './store.js'

const log = pino({ name: 'pair-api' })

export function installPairingApi(app: express.Express) {
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
			res.json({ ok: true, pair_code: rec.code, deep_link, expires_at: rec.expires_at })
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
			res.json({ ok: true, pair_code: rec.code, expires_at: rec.expires_at })
		} catch (e) {
			log.error({ tag: 'PAIR', route: 'create.v1', err: String(e) }, '[PAIR] v1/create: error')
			res.status(500).json({ ok: false })
		}
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
		res.json({ ok: true, state: rec.state, expires_in: ttl })
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
