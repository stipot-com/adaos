import express from 'express'
import pino from 'pino'
import { verifyHubToken } from '../../db/tg.repo.js'

const log = pino({ name: 'nats-authz' })

type AuthzRequest = {
  jwt?: string
  connect_opts?: { user?: string, pass?: string }
  user?: string
  pass?: string
}

function getPerms(hubId: string) {
  return {
    pub: ['tg.output.*', 'route.to_browser.*'],
    sub: [`tg.input.${hubId}`, 'route.to_hub.*'],
  }
}

function maskToken(tok?: string): string | undefined {
  if (!tok) return tok
  if (tok.length <= 6) return '***'
  return tok.slice(0, 3) + '***' + tok.slice(-2)
}

export async function installNatsAuth(app: express.Express) {
  app.post('/_health/internal', (_req, res) => res.json({ ok: true }))

  app.post('/internal/nats/authz', async (req, res) => {
    try {
      const body = req.body as AuthzRequest
      const co = body?.connect_opts || {}
      const userRaw = (co.user || (body as any)?.user || '').trim()
      const passRaw = (co.pass || (body as any)?.pass || '').trim()
      const rip = (req.headers['x-forwarded-for'] as string) || req.socket.remoteAddress || ''
      log.info({ from: rip, user: userRaw, pass: maskToken(passRaw) }, 'authz: request')
      if (!userRaw || !passRaw) {
        log.warn({ have_user: !!userRaw, have_pass: !!passRaw }, 'authz: missing creds')
        return res.status(401).json({ error: 'missing_credentials' })
      }
      const hubId = userRaw.startsWith('hub_') ? userRaw.slice(4) : userRaw
      const ok = await verifyHubToken(hubId, passRaw)
      if (!ok) {
        log.warn({ hub_id: hubId, user: userRaw, pass: maskToken(passRaw) }, 'authz: invalid token')
        return res.status(403).json({ error: 'invalid_credentials' })
      }

      // Build a NATS user JWT signed by issuer seed
      const issuerSeed = process.env['NATS_ISSUER_SEED'] || ''
      const issuerPub = process.env['NATS_ISSUER_PUB'] || ''
      if (!issuerSeed || !issuerPub) {
        log.warn({ have_seed: !!issuerSeed, have_pub: !!issuerPub }, 'authz: issuer material missing')
        return res.status(500).json({ error: 'issuer_missing' })
      }

      // dynamic import to avoid hard dep if not configured
      let jwtMod: any, nkeysMod: any
      try {
        // eslint-disable-next-line no-new-func
        try { jwtMod = await (new Function('m', 'return import(m)'))('@nats-io/jwt') } catch { /* try alt pkg */ }
        if (!jwtMod) { jwtMod = await (new Function('m', 'return import(m)'))('nats-jwt') }
        // eslint-disable-next-line no-new-func
        try { nkeysMod = await (new Function('m', 'return import(m)'))('@nats-io/nkeys.js') } catch { /* try alt pkg */ }
        if (!nkeysMod) { nkeysMod = await (new Function('m', 'return import(m)'))('nkeys.js') }
      } catch (e) {
        log.error({ err: String(e) }, 'authz: nats jwt/nkeys import failed')
        return res.status(500).json({ error: 'jwt_lib_unavailable' })
      }

      const perms = getPerms(hubId)
      try {
        const kp = nkeysMod.fromSeed(issuerSeed)
        const pub = kp.getPublicKey()
        let userJwt: string | undefined
        // Attempt API for @nats-io/jwt style
        if (jwtMod.UserJWT) {
          const uj = new jwtMod.UserJWT()
          uj.issuer = pub
          uj.name = `hub_${hubId}`
          uj.sub = pub
          uj.audience = 'APP'
          uj.tags = ['hub']
          uj.permissions = { publish: { allow: perms.pub }, subscribe: { allow: perms.sub } }
          userJwt = uj.encode(kp)
        } else if (jwtMod.User) {
          // Fallback for nats-jwt package API
          const u = new jwtMod.User()
          u.issuer = pub
          u.name = `hub_${hubId}`
          u.sub = pub
          u.audience = 'APP'
          u.tags = ['hub']
          u.permissions = { publish: { allow: perms.pub }, subscribe: { allow: perms.sub } }
          userJwt = u.encode(kp)
        } else if (jwtMod.createUserJWT) {
          userJwt = await jwtMod.createUserJWT({
            issuer: pub,
            name: `hub_${hubId}`,
            sub: pub,
            audience: 'APP',
            tags: ['hub'],
            permissions: { publish: { allow: perms.pub }, subscribe: { allow: perms.sub } },
          }, kp)
        }
        if (!userJwt) throw new Error('jwt_api_unsupported')
        log.info({ hub_id: hubId, from: rip, pub_allow: perms.pub, sub_allow: perms.sub }, 'authz: ok')
        return res.json({ jwt: userJwt })
      } catch (e) {
        log.error({ hub_id: hubId, err: String(e) }, 'authz: jwt build failed')
        return res.status(500).json({ error: 'jwt_build_failed' })
      }

    } catch (e) {
      log.error({ err: String(e) }, 'authz: failure')
      return res.status(500).json({ error: 'internal_error' })
    }
  })
}
