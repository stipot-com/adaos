import express from 'express'
import pino from 'pino'
import { Algorithms, type AuthorizationRequest, type ClaimsData, type Permissions, decode, encodeAuthorizationResponse, encodeUser, fromPublic, fromSeed } from '@nats-io/jwt'
import { connect, type Msg, type NatsConnection } from 'nats'
import { verifyHubToken } from '../../db/tg.repo.js'

const log = pino({ name: 'nats-authz' })

const AUTH_CALLOUT_SUBJECT = '$SYS.REQ.USER.AUTH'
const AUTH_CALLOUT_QUEUE = 'inimatic-auth-callout'
const NON_OPERATOR_TARGET_ACCOUNT = '$G'
const textDecoder = new TextDecoder()
const textEncoder = new TextEncoder()

let calloutStarted = false

type AuthzRequest = {
	user_nkey?: string
	server_id?: string
	connect_opts?: { user?: string, pass?: string }
	user?: string
	pass?: string
}

type VerifiedHubCreds = {
	hubId: string
	user: string
	pass: string
}

function getPerms(hubId: string): Permissions {
	return {
		pub: { allow: ['tg.output.*', 'route.to_browser.*'] },
		sub: { allow: [`tg.input.${hubId}`, 'route.to_hub.*'] },
	}
}

function maskToken(tok?: string): string | undefined {
	if (!tok) return tok
	if (tok.length <= 6) return '***'
	return tok.slice(0, 3) + '***' + tok.slice(-2)
}

function sleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms))
}

function natsAuthConfig() {
	const servers = String(process.env['NATS_URL'] || 'nats://nats:4222').trim() || 'nats://nats:4222'
	const user = String(process.env['NATS_USER'] || '').trim()
	const pass = String(process.env['NATS_PASS'] || '').trim()
	const issuerSeed = String(process.env['NATS_ISSUER_SEED'] || '').trim()
	const issuerPub = String(process.env['NATS_ISSUER_PUB'] || '').trim()
	return { servers, user, pass, issuerSeed, issuerPub }
}

function issuerKeyPair() {
	const cfg = natsAuthConfig()
	if (!cfg.issuerSeed || !cfg.issuerPub) {
		throw new Error('nats_issuer_material_missing')
	}
	const kp = fromSeed(textEncoder.encode(cfg.issuerSeed))
	const actualPub = kp.getPublicKey()
	if (actualPub !== cfg.issuerPub) {
		throw new Error(`nats_issuer_mismatch:${actualPub}`)
	}
	return kp
}

async function verifyHubCredentials(userRaw: string, passRaw: string): Promise<VerifiedHubCreds | null> {
	const user = String(userRaw || '').trim()
	const pass = String(passRaw || '').trim()
	if (!user || !pass) {
		return null
	}
	const hubId = user.startsWith('hub_') ? user.slice(4) : user
	const ok = await verifyHubToken(hubId, pass)
	if (!ok) {
		return null
	}
	return { hubId, user, pass }
}

async function issueUserJwt(opts: { hubId: string, userNkey: string }): Promise<string> {
	const issuer = issuerKeyPair()
	return await encodeUser(
		`hub_${opts.hubId}`,
		fromPublic(opts.userNkey),
		issuer,
		getPerms(opts.hubId),
		{
			algorithm: Algorithms.v2,
			aud: NON_OPERATOR_TARGET_ACCOUNT,
		}
	)
}

async function issueAuthResponse(opts: {
	userNkey: string
	serverId: string
	hubId?: string
	error?: string
}): Promise<string> {
	const issuer = issuerKeyPair()
	const payload = opts.error
		? { error: opts.error }
		: { jwt: await issueUserJwt({ hubId: String(opts.hubId || ''), userNkey: opts.userNkey }) }
	return await encodeAuthorizationResponse(
		fromPublic(opts.userNkey),
		fromPublic(opts.serverId),
		issuer,
		payload,
		{ algorithm: Algorithms.v2 }
	)
}

async function respondWithError(msg: Msg, userNkey: string | undefined, serverId: string | undefined, error: string): Promise<void> {
	if (!userNkey || !serverId) {
		log.warn({ err: error }, 'authz: cannot encode error response without user/server nkeys')
		return
	}
	const token = await issueAuthResponse({ userNkey, serverId, error })
	msg.respond(textEncoder.encode(token))
}

async function handleCalloutMsg(msg: Msg): Promise<void> {
	let userNkey: string | undefined
	let serverId: string | undefined
	try {
		const serverXKey = String(msg.headers?.get('Nats-Server-Xkey') || '').trim()
		if (serverXKey) {
			await respondWithError(msg, undefined, undefined, 'xkey_not_supported')
			return
		}

		const token = textDecoder.decode(msg.data)
		const claims = decode<AuthorizationRequest>(token) as ClaimsData<AuthorizationRequest>
		const req = claims.nats || {}
		userNkey = String(req.user_nkey || claims.sub || '').trim()
		serverId = String(req.server_id?.id || claims.iss || '').trim()
		const userRaw = String(req.connect_opts?.user || req.client_info?.user || '').trim()
		const passRaw = String(req.connect_opts?.pass || '').trim()

		if (!userNkey || !serverId) {
			throw new Error('missing_callout_nkeys')
		}

		const verified = await verifyHubCredentials(userRaw, passRaw)
		if (!verified) {
			log.warn({ user: userRaw, pass: maskToken(passRaw) }, 'authz: invalid credentials')
			await respondWithError(msg, userNkey, serverId, 'invalid_credentials')
			return
		}

		const response = await issueAuthResponse({
			userNkey,
			serverId,
			hubId: verified.hubId,
		})
		msg.respond(textEncoder.encode(response))
		log.info({ hub_id: verified.hubId, user: verified.user }, 'authz: callout ok')
	} catch (error) {
		const err = error instanceof Error ? error : new Error(String(error))
		log.error({ err: err.message, user_nkey: userNkey, server_id: serverId }, 'authz: callout failed')
		try {
			await respondWithError(msg, userNkey, serverId, 'internal_error')
		} catch (replyError) {
			log.error({ err: String(replyError) }, 'authz: failed to send error response')
		}
	}
}

async function runCalloutLoop(): Promise<void> {
	const cfg = natsAuthConfig()
	if (!cfg.user || !cfg.pass || !cfg.issuerSeed || !cfg.issuerPub) {
		log.warn(
			{
				have_user: !!cfg.user,
				have_pass: !!cfg.pass,
				have_issuer_seed: !!cfg.issuerSeed,
				have_issuer_pub: !!cfg.issuerPub,
			},
			'authz: callout disabled; incomplete config'
		)
		return
	}

	let backoffMs = 1_000
	while (true) {
		let nc: NatsConnection | null = null
		try {
			nc = await connect({
				servers: cfg.servers,
				user: cfg.user,
				pass: cfg.pass,
				name: 'inimatic-nats-auth-callout',
			})
			log.info({ server: cfg.servers, user: cfg.user, subject: AUTH_CALLOUT_SUBJECT }, 'authz: callout connected')
			backoffMs = 1_000

			const sub = nc.subscribe(AUTH_CALLOUT_SUBJECT, { queue: AUTH_CALLOUT_QUEUE })
			for await (const msg of sub) {
				await handleCalloutMsg(msg)
			}

			const closedErr = await nc.closed()
			if (closedErr) {
				throw closedErr
			}
			throw new Error('callout_subscription_stopped')
		} catch (error) {
			log.error({ err: String(error), retry_ms: backoffMs }, 'authz: callout disconnected')
		} finally {
			try {
				nc?.close()
			} catch { }
		}
		await sleep(backoffMs)
		backoffMs = Math.min(backoffMs * 2, 30_000)
	}
}

function ensureCalloutStarted(): void {
	if (calloutStarted) {
		return
	}
	calloutStarted = true
	void runCalloutLoop()
}

export async function installNatsAuth(app: express.Express) {
	app.post('/_health/internal', (_req, res) => res.json({ ok: true }))

	app.post('/internal/nats/authz', async (req, res) => {
		try {
			const body = req.body as AuthzRequest
			const co = body?.connect_opts || {}
			const userRaw = String(co.user || body?.user || '').trim()
			const passRaw = String(co.pass || body?.pass || '').trim()
			const userNkey = String(body?.user_nkey || '').trim()
			const serverId = String(body?.server_id || '').trim()
			const rip = (req.headers['x-forwarded-for'] as string) || req.socket.remoteAddress || ''
			log.info({ from: rip, user: userRaw, pass: maskToken(passRaw) }, 'authz: http request')

			const verified = await verifyHubCredentials(userRaw, passRaw)
			if (!verified) {
				return res.status(403).json({ ok: false, error: 'invalid_credentials' })
			}

			const payload: Record<string, unknown> = {
				ok: true,
				hub_id: verified.hubId,
				permissions: getPerms(verified.hubId),
			}
			if (userNkey && serverId) {
				payload['auth_response_jwt'] = await issueAuthResponse({
					userNkey,
					serverId,
					hubId: verified.hubId,
				})
			}
			return res.json(payload)
		} catch (error) {
			log.error({ err: String(error) }, 'authz: http failure')
			return res.status(500).json({ ok: false, error: 'internal_error' })
		}
	})

	ensureCalloutStarted()
}
