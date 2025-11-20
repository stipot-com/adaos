// src/adaos/integrations/inimatic/backend/webauthn.ts
import type express from 'express'
import type { RedisClientType } from 'redis'
import { randomBytes } from 'node:crypto'

// NOTE: для реальной валидации WebAuthn рекомендуется использовать @simplewebauthn/server.
// Здесь интерфейс спроектирован так, чтобы было легко подключить библиотеку позже.

export type WebSessionStage =
	| 'NEW'
	| 'PREAUTH'
	| 'WEBREG'
	| 'AUTH'
	| 'PAIRED'
	| 'ONLINE'

export interface WebSessionState {
	sid: string
	owner_id?: string
	subnet_id?: string
	hub_id?: string
	browser_key_id?: string
	stage: WebSessionStage
	exp: number // unix timestamp (seconds)
}

export interface DeviceCodeRecord {
	device_code: string
	user_code: string
	owner_id: string
	subnet_id?: string
	hub_id?: string
	exp: number
	bind_sid?: string
}

export interface WebAuthnCredentialRecord {
	cred_id: string
	owner_id: string
	browser_pubkey?: unknown
	sign_count: number
}

export interface WebAuthnDeps {
	// any-any-any, чтобы принять клиент с доп. модулями (graph и т.п.)
	redis: RedisClientType<any, any, any>
	rpID: string
	origin: string
	defaultSessionTtlSeconds: number
}

export interface WebAuthnService {
	verifyDeviceCodeLogin(
		deviceCodeOrUserCode: string,
		sid: string
	): Promise<{
		ok: boolean
		owner_id?: string
		subnet_id?: string
		hub_id?: string
		error?: 'invalid_device_code' | 'expired_token'
	}>
	createRegistrationChallenge(
		sid: string
	): Promise<{
		ok: boolean
		publicKeyCredentialCreationOptions?: any
		error?: 'session_not_found' | 'invalid_state'
	}>
	finishRegistration(
		sid: string,
		credential: any
	): Promise<{
		ok: boolean
		browser_key_id?: string
		error?: 'session_not_found' | 'invalid_state' | 'verification_failed'
	}>
	createLoginChallenge(
		sid: string
	): Promise<{
		ok: boolean
		publicKeyCredentialRequestOptions?: any
		error?: 'session_not_found' | 'registration_required'
	}>
	finishLogin(
		sid: string,
		credential: any
	): Promise<{
		ok: boolean
		session_jwt?: string
		browser_key_id?: string
		error?: 'session_not_found' | 'assertion_failed'
	}>
}

function nowSeconds(): number {
	return Math.floor(Date.now() / 1000)
}

function sessionKey(sid: string): string {
	return `session:web:${sid}`
}

function deviceCodeKey(code: string): string {
	return `device_code:${code}`
}

function webAuthnCredKey(credId: string): string {
	return `webauthn:cred:${credId}`
}

function randomToken(prefix: string): string {
	return `${prefix}_${randomBytes(24).toString('hex')}`
}

async function loadSession(
	redis: RedisClientType<any, any, any>,
	sid: string
): Promise<WebSessionState | null> {
	const key = sessionKey(sid)
	const raw = await redis.get(key)
	if (!raw) return null
	try {
		const parsed = JSON.parse(raw) as WebSessionState
		return parsed
	} catch {
		return null
	}
}

async function saveSession(
	redis: RedisClientType<any, any, any>,
	session: WebSessionState
): Promise<void> {
	const key = sessionKey(session.sid)
	const ttl = Math.max(60, session.exp - nowSeconds())
	await redis.setEx(key, ttl, JSON.stringify(session))
}

async function findDeviceCodeByUserOrDevice(
	redis: RedisClientType<any, any, any>,
	code: string
): Promise<DeviceCodeRecord | null> {
	// Основной ключ — device_code:{device_code}. Но CLI показывает user_code, поэтому
	// сначала пробуем device_code, затем делаем скан по user_code.
	const direct = await redis.get(deviceCodeKey(code))
	if (direct) {
		try {
			return JSON.parse(direct) as DeviceCodeRecord
		} catch {
			return null
		}
	}

	// Фоллбэк по user_code: небольшой SCAN по device_code:*
	// Для dev-сценария допустимо, для продакшена лучше хранить обратный индекс.
	const iterator = redis.scanIterator({
		MATCH: 'device_code:*',
		COUNT: 100,
	}) as AsyncIterable<string>
	for await (const key of iterator) {
		const raw = await redis.get(key)
		if (!raw) continue
		try {
			const rec = JSON.parse(raw) as DeviceCodeRecord
			if (rec.user_code === code) {
				return rec
			}
		} catch {
			// ignore
		}
	}
	return null
}

export async function storeDeviceCode(
	redis: RedisClientType<any, any, any>,
	record: DeviceCodeRecord,
	ttlSeconds?: number
): Promise<void> {
	const ttl = ttlSeconds ?? Math.max(1, record.exp - nowSeconds())
	await redis.setEx(
		deviceCodeKey(record.device_code),
		ttl,
		JSON.stringify(record)
	)
}

export function createWebAuthnService(deps: WebAuthnDeps): WebAuthnService {
	const { redis, rpID, origin, defaultSessionTtlSeconds } = deps

	return {
		async verifyDeviceCodeLogin(deviceCodeOrUserCode: string, sid: string) {
			const rec = await findDeviceCodeByUserOrDevice(
				redis,
				deviceCodeOrUserCode
			)
			if (!rec) {
				return {
					ok: false as const,
					error: 'invalid_device_code' as const,
				}
			}
			if (rec.exp <= nowSeconds()) {
				await redis.del(deviceCodeKey(rec.device_code))
				return { ok: false as const, error: 'expired_token' as const }
			}

			const sessionTtl = defaultSessionTtlSeconds
			const session: WebSessionState = {
				sid,
				owner_id: rec.owner_id,
				subnet_id: rec.subnet_id,
				hub_id: rec.hub_id,
				stage: 'PREAUTH',
				exp: nowSeconds() + sessionTtl,
			}

			await saveSession(redis, session)

			// помечаем, что код привязан к sid
			rec.bind_sid = sid
			await redis.setEx(
				deviceCodeKey(rec.device_code),
				Math.max(30, rec.exp - nowSeconds()),
				JSON.stringify(rec)
			)

			return {
				ok: true as const,
				owner_id: rec.owner_id,
				subnet_id: rec.subnet_id,
				hub_id: rec.hub_id,
			}
		},

		async createRegistrationChallenge(sid: string) {
			const session = await loadSession(redis, sid)
			if (!session) {
				return {
					ok: false as const,
					error: 'session_not_found' as const,
				}
			}
			if (!session.owner_id) {
				return { ok: false as const, error: 'invalid_state' as const }
			}

			const userIdBytes = Buffer.from(session.owner_id, 'utf8')
			const challenge = randomBytes(32).toString('base64url')

			// сохраняем challenge для последующей верификации (упрощённо — как часть session)
			const updated: WebSessionState & { webreg_challenge?: string } = {
				...session,
				stage: 'WEBREG',
				webreg_challenge: challenge,
			} as any
			await saveSession(redis, updated)

			const publicKeyCredentialCreationOptions = {
				rp: {
					id: rpID,
					name: 'Inimatic AdaOS',
				},
				user: {
					id: userIdBytes,
					name: session.owner_id,
					displayName: session.owner_id,
				},
				challenge: Buffer.from(challenge, 'base64url'),
				pubKeyCredParams: [
					{ type: 'public-key', alg: -7 }, // ES256
					{ type: 'public-key', alg: -8 }, // EdDSA (на будущее)
				],
				timeout: 60_000,
				attestation: 'none',
				authenticatorSelection: {
					authenticatorAttachment: 'platform',
					userVerification: 'required',
				},
			}

			return { ok: true as const, publicKeyCredentialCreationOptions }
		},

		async finishRegistration(sid: string, credential: any) {
			const session = (await loadSession(redis, sid)) as
				| (WebSessionState & { webreg_challenge?: string })
				| null
			if (!session) {
				return {
					ok: false as const,
					error: 'session_not_found' as const,
				}
			}
			if (!session.owner_id || session.stage !== 'WEBREG') {
				return { ok: false as const, error: 'invalid_state' as const }
			}

			// TODO: здесь должна быть реальная проверка attestation с помощью @simplewebauthn/server.
			// Пока примем всё как валидное и просто сохраним credentialId.
			try {
				const credId: string | undefined = credential?.id
				if (!credId || typeof credId !== 'string') {
					return {
						ok: false as const,
						error: 'verification_failed' as const,
					}
				}

				const record: WebAuthnCredentialRecord = {
					cred_id: credId,
					owner_id: session.owner_id,
					browser_pubkey: undefined,
					sign_count: 0,
				}
				await redis.set(webAuthnCredKey(credId), JSON.stringify(record))

				const updated: WebSessionState = {
					...session,
					browser_key_id: credId,
					stage: 'AUTH', // после регистрации сразу считаем, что можем логиниться
				}
				delete (updated as any).webreg_challenge
				await saveSession(redis, updated)

				return { ok: true as const, browser_key_id: credId }
			} catch {
				return {
					ok: false as const,
					error: 'verification_failed' as const,
				}
			}
		},

		async createLoginChallenge(sid: string) {
			const session = await loadSession(redis, sid)
			if (!session) {
				return {
					ok: false as const,
					error: 'session_not_found' as const,
				}
			}
			if (!session.browser_key_id) {
				return {
					ok: false as const,
					error: 'registration_required' as const,
				}
			}

			const challenge = randomBytes(32).toString('base64url')
			const updated: WebSessionState & { weblogin_challenge?: string } = {
				...session,
				weblogin_challenge: challenge,
			} as any
			await saveSession(redis, updated)

			const publicKeyCredentialRequestOptions = {
				challenge: Buffer.from(challenge, 'base64url'),
				rpId: rpID,
				allowCredentials: [
					{
						id: Buffer.from(session.browser_key_id, 'utf8'),
						type: 'public-key',
						transports: ['internal'],
					},
				],
				userVerification: 'required',
				timeout: 60_000,
			}

			return { ok: true as const, publicKeyCredentialRequestOptions }
		},

		async finishLogin(sid: string, credential: any) {
			const session = (await loadSession(redis, sid)) as
				| (WebSessionState & { weblogin_challenge?: string })
				| null
			if (!session) {
				return {
					ok: false as const,
					error: 'session_not_found' as const,
				}
			}
			if (!session.browser_key_id) {
				return {
					ok: false as const,
					error: 'assertion_failed' as const,
				}
			}

			// TODO: реальная проверка assertion через @simplewebauthn/server.
			try {
				const credId: string | undefined = credential?.id
				if (!credId || credId !== session.browser_key_id) {
					return {
						ok: false as const,
						error: 'assertion_failed' as const,
					}
				}

				const recordRaw = await redis.get(webAuthnCredKey(credId))
				if (recordRaw) {
					try {
						const rec = JSON.parse(
							recordRaw
						) as WebAuthnCredentialRecord
						rec.sign_count += 1
						await redis.set(
							webAuthnCredKey(credId),
							JSON.stringify(rec)
						)
					} catch {
						// ignore
					}
				}

				// Упрощённый session_jwt: для dev — случайный токен, который фронт будет использовать как bearer
				const session_jwt = randomToken('sess')
				const loginSession: WebSessionState = {
					...session,
					stage: 'AUTH',
					exp: nowSeconds() + defaultSessionTtlSeconds,
				}
				delete (loginSession as any).weblogin_challenge
				await saveSession(redis, loginSession)

				// В реальной реализации session_jwt нужно положить в Redis
				await redis.setEx(
					`session:jwt:${session_jwt}`,
					defaultSessionTtlSeconds,
					JSON.stringify({
						sid,
						owner_id: session.owner_id,
						browser_key_id: session.browser_key_id,
					})
				)

				return {
					ok: true as const,
					session_jwt,
					browser_key_id: session.browser_key_id,
				}
			} catch {
				return {
					ok: false as const,
					error: 'assertion_failed' as const,
				}
			}
		},
	}
}

export function extractSid(body: unknown): string | null {
	if (!body || typeof body !== 'object') return null
	const sid = (body as any).sid
	if (typeof sid !== 'string' || !sid.trim()) return null
	return sid.trim()
}

export function extractDeviceCode(body: unknown): string | null {
	if (!body || typeof body !== 'object') return null
	const b = body as any
	const device_code = (b.device_code ?? b.user_code ?? b.code) as unknown
	if (typeof device_code !== 'string' || !device_code.trim()) return null
	return device_code.trim()
}

export type ErrorResponder = (
	req: express.Request,
	res: express.Response,
	status: number,
	code: string,
	params?: Record<string, any>
) => void

export function installWebAuthnRoutes(
	app: express.Express,
	deps: WebAuthnDeps,
	respondError: ErrorResponder
): void {
	const service = createWebAuthnService(deps)

	// POST /v1/owner/login/verify { device_code, sid }
	app.post('/v1/owner1/login/verify', async (req, res) => {
		try {
			const sid = extractSid(req.body)
			const code = extractDeviceCode(req.body)
			if (!sid) {
				return respondError(req, res, 400, 'missing_field', {
					field: 'sid',
				})
			}
			if (!code) {
				return respondError(req, res, 400, 'missing_field', {
					field: 'device_code',
				})
			}
			const result = await service.verifyDeviceCodeLogin(code, sid)
			if (!result.ok) {
				const status =
					result.error === 'invalid_device_code' ? 400 : 400
				const errCode = result.error ?? 'invalid_device_code'
				return respondError(req, res, status, errCode)
			}
			res.json({
				ok: true,
				owner_id: result.owner_id,
				subnet_id: result.subnet_id,
				hub_id: result.hub_id,
			})
		} catch (error) {
			console.error('webauthn.login.verify error', error)
			respondError(req, res, 500, 'internal_error')
		}
	})

	// POST /v1/owner/webauthn/registration/challenge { sid }
	app.post('/v1/owner1/webauthn/registration/challenge', async (req, res) => {
		try {
			const sid = extractSid(req.body)
			if (!sid) {
				return respondError(req, res, 400, 'missing_field', {
					field: 'sid',
				})
			}
			const result = await service.createRegistrationChallenge(sid)
			if (!result.ok) {
				const code =
					result.error === 'session_not_found'
						? 'invalid_device_code'
						: 'invalid_request'
				return respondError(req, res, 400, code)
			}
			res.json({
				publicKeyCredentialCreationOptions:
					result.publicKeyCredentialCreationOptions,
			})
		} catch (error) {
			console.error('webauthn.registration.challenge error', error)
			respondError(req, res, 500, 'internal_error')
		}
	})

	// POST /v1/owner/webauthn/registration/finish { sid, credential }
	app.post('/v1/owner1/webauthn/registration/finish', async (req, res) => {
		try {
			const sid = extractSid(req.body)
			const credential = (req.body as any)?.credential
			if (!sid) {
				return respondError(req, res, 400, 'missing_field', {
					field: 'sid',
				})
			}
			if (!credential) {
				return respondError(req, res, 400, 'missing_field', {
					field: 'credential',
				})
			}
			const result = await service.finishRegistration(sid, credential)
			if (!result.ok) {
				const codeMap: Record<string, string> = {
					session_not_found: 'invalid_device_code',
					invalid_state: 'invalid_request',
					verification_failed: 'invalid_request',
				}
				const code = codeMap[result.error!] ?? 'invalid_request'
				return respondError(req, res, 400, code)
			}
			res.json({ browser_key_id: result.browser_key_id })
		} catch (error) {
			console.error('webauthn.registration.finish error', error)
			respondError(req, res, 500, 'internal_error')
		}
	})

	// POST /v1/owner/webauthn/login/challenge { sid }
	app.post('/v1/owner1/webauthn/login/challenge', async (req, res) => {
		try {
			const sid = extractSid(req.body)
			if (!sid) {
				return respondError(req, res, 400, 'missing_field', {
					field: 'sid',
				})
			}
			const result = await service.createLoginChallenge(sid)
			if (!result.ok) {
				const code =
					result.error === 'registration_required'
						? 'invalid_device_code'
						: 'invalid_request'
				return respondError(req, res, 400, code)
			}
			res.json({
				publicKeyCredentialRequestOptions:
					result.publicKeyCredentialRequestOptions,
			})
		} catch (error) {
			console.error('webauthn.login.challenge error', error)
			respondError(req, res, 500, 'internal_error')
		}
	})

	// POST /v1/owner/webauthn/login/finish { sid, credential }
	app.post('/v1/owner1/webauthn/login/finish', async (req, res) => {
		try {
			const sid = extractSid(req.body)
			const credential = (req.body as any)?.credential
			if (!sid) {
				return respondError(req, res, 400, 'missing_field', {
					field: 'sid',
				})
			}
			if (!credential) {
				return respondError(req, res, 400, 'missing_field', {
					field: 'credential',
				})
			}
			const result = await service.finishLogin(sid, credential)
			if (!result.ok) {
				return respondError(req, res, 400, 'invalid_request')
			}
			res.json({
				session_jwt: result.session_jwt,
				browser_key_id: result.browser_key_id,
			})
		} catch (error) {
			console.error('webauthn.login.finish error', error)
			respondError(req, res, 500, 'internal_error')
		}
	})
}
