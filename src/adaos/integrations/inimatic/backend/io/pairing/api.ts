import express from 'express'
import { pairConfirm, pairCreate, pairGet, pairRevoke, bindingUpsert } from './store.js'

export function installPairingApi(app: express.Express) {
  app.post('/io/tg/pair/create', async (req, res) => {
    const hub = typeof req.query['hub'] === 'string' ? (req.query['hub'] as string) : undefined
    const ttl = Number.parseInt(String(req.query['ttl'] ?? '600'), 10) || 600
    const bot = typeof req.query['bot'] === 'string' ? (req.query['bot'] as string) : (process.env['BOT_ID'] || 'main-bot')
    const rec = await pairCreate(bot, hub, ttl)
    const deep_link = process.env['BOT_USERNAME'] ? `https://t.me/${process.env['BOT_USERNAME']}?start=${rec.code}` : undefined
    res.json({ ok: true, pair_code: rec.code, deep_link, expires_at: rec.expires_at })
  })

  app.post('/io/tg/pair/confirm', async (req, res) => {
    const code = String(req.body?.code || req.query['code'] || '')
    if (!code) return res.status(400).json({ error: 'code_required' })
    const rec = await pairConfirm(code)
    if (!rec) return res.status(404).json({ ok: false, error: 'not_found' })
    if (rec.state === 'expired') return res.status(400).json({ ok: false, error: 'expired' })
    if (rec.state === 'revoked') return res.status(400).json({ ok: false, error: 'revoked' })
    const user_id = String(req.body?.user_id || req.query['user_id'] || '')
    const bot_id = String(req.body?.bot_id || req.query['bot_id'] || rec.bot_id || '')
    const binding = await bindingUpsert('telegram', user_id, bot_id, rec.hub_id)
    res.json({ ok: true, hub_id: binding.hub_id, ada_user_id: binding.ada_user_id })
  })

  app.get('/io/tg/pair/status', async (req, res) => {
    const code = String(req.query['code'] || '')
    if (!code) return res.status(400).json({ error: 'code_required' })
    const rec = await pairGet(code)
    if (!rec) return res.json({ ok: true, state: 'not_found' })
    const ttl = Math.max(0, rec.expires_at - Math.floor(Date.now() / 1000))
    res.json({ ok: true, state: rec.state, expires_in: ttl })
  })

  app.post('/io/tg/pair/revoke', async (req, res) => {
    const code = String(req.body?.code || req.query['code'] || '')
    if (!code) return res.status(400).json({ error: 'code_required' })
    const ok = await pairRevoke(code)
    res.json({ ok })
  })
}

