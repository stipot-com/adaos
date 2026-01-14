// src/adaos/integrations/inimatic/backend/webauthn.ts
import type express from 'express'
import type { RedisClientType } from 'redis'
import { randomBytes } from 'node:crypto'
import { signWebSessionJwt } from './sessionJwt.js'

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

export interface PairingCodeRecord {
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
	created_at?: number // timestamp в миллисекундах
}

export interface WebAuthnDeps {
	// any-any-any, чтобы принять клиент с доп. модулями (graph и т.п.)
	redis: RedisClientType<any, any, any>
	rpID: string
	origin: string
	defaultSessionTtlSeconds: number
	sessionJwtSecret: string
}

export interface WebAuthnService {
	verifyDeviceCodeLogin(
		userCode: string,
		sid: string
	): Promise<{
		ok: boolean
		owner_id?: string
		subnet_id?: string
		hub_id?: string
		error?: 'invalid_user_code' | 'expired_token'
	}>
	createRegistrationChallenge(sid: string): Promise<{
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
	createLoginChallenge(sid: string): Promise<{
		ok: boolean
		publicKeyCredentialRequestOptions?: any
		error?: 'session_not_found' | 'registration_required'
	}>
	createLoginChallengeByOwnerId(owner_id: string): Promise<{
		ok: boolean
		publicKeyCredentialRequestOptions?: any
		allowCredentials?: Array<{ id: string; type: string }>
		error?: 'owner_not_found' | 'no_credentials_registered'
	}>
	createLoginChallengeAuto(): Promise<{
		ok: boolean
		publicKeyCredentialRequestOptions?: any
		error?: never
	}>
	finishLogin(
		sid: string | undefined,
		credential: any,
		challenge: string
	): Promise<{
		ok: boolean
		session_jwt?: string
		browser_key_id?: string
		owner_id?: string
		error?: 'session_not_found' | 'assertion_failed' | 'challenge_invalid'
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

function ownerCredentialsKey(ownerId: string): string {
	return `webauthn:owner:${ownerId}:creds`
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

async function getCredentialsForOwner(
	redis: RedisClientType<any, any, any>,
	owner_id: string
): Promise<WebAuthnCredentialRecord[]> {
	const key = ownerCredentialsKey(owner_id)
	const raw = await redis.get(key)
	if (!raw) return []
	try {
		return JSON.parse(raw) as WebAuthnCredentialRecord[]
	} catch {
		return []
	}
}

async function addCredentialForOwner(
	redis: RedisClientType<any, any, any>,
	owner_id: string,
	credential: WebAuthnCredentialRecord
): Promise<void> {
	const credentials = await getCredentialsForOwner(redis, owner_id)
	credentials.push(credential)
	const key = ownerCredentialsKey(owner_id)
	await redis.set(key, JSON.stringify(credentials))
}

async function findDeviceCodeByUserOrDevice(
	redis: RedisClientType<any, any, any>,
	code: string
): Promise<PairingCodeRecord | null> {
	// Основной ключ — device_code:{device_code}. Но CLI показывает user_code, поэтому
	// сначала пробуем device_code, затем делаем скан по user_code.
	const direct = await redis.get(deviceCodeKey(code))
	if (direct) {
		try {
			return JSON.parse(direct) as PairingCodeRecord
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
			const rec = JSON.parse(raw) as PairingCodeRecord
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
	record: PairingCodeRecord,
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
	const { redis, rpID, origin, defaultSessionTtlSeconds, sessionJwtSecret } = deps

	return {
		async verifyDeviceCodeLogin(userCode: string, sid: string) {
			const rec = await findDeviceCodeByUserOrDevice(redis, userCode)
			if (!rec) {
				return {
					ok: false as const,
					error: 'invalid_user_code' as const,
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
			console.log('[webauthn] Session saved:', {
				sid,
				owner_id: rec.owner_id,
				key: sessionKey(sid),
			})

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
			console.log('[webauthn] Loading session for registration:', {
				sid,
				key: sessionKey(sid),
			})
			const session = await loadSession(redis, sid)
			if (!session) {
				console.warn('[webauthn] Session not found for registration:', {
					sid,
					key: sessionKey(sid),
				})
				return {
					ok: false as const,
					error: 'session_not_found' as const,
				}
			}
			if (!session.owner_id) {
				return { ok: false as const, error: 'invalid_state' as const }
			}

			const challenge = randomBytes(32).toString('base64url')
			const userIdBase64url = Buffer.from(
				session.owner_id,
				'utf8'
			).toString('base64url')

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
					id: userIdBase64url,
					name: session.owner_id,
					displayName: session.owner_id,
				},
				challenge: challenge,
				pubKeyCredParams: [
					{ type: 'public-key', alg: -7 }, // ES256
					{ type: 'public-key', alg: -257 }, // RS256
					{ type: 'public-key', alg: -8 }, // EdDSA
				],
				timeout: 60_000,
				attestation: 'none',
				excludeCredentials: [],
				authenticatorSelection: {
					residentKey: 'preferred',
					userVerification: 'preferred',
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
					created_at: Date.now(),
				}
				await redis.set(webAuthnCredKey(credId), JSON.stringify(record))
				// Сохраняем credential в списке для owner_id
				await addCredentialForOwner(redis, session.owner_id, record)

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

			const credentialIdBase64url = Buffer.from(
				session.browser_key_id,
				'utf8'
			).toString('base64url')

			const publicKeyCredentialRequestOptions = {
				challenge: challenge,
				rpId: rpID,
				allowCredentials: [
					// {
					// 	id: credentialIdBase64url,
					// 	type: 'public-key',
					// 	transports: ['internal'],
					// },
				],
				userVerification: 'required',
				timeout: 60_000,
			}

			return { ok: true as const, publicKeyCredentialRequestOptions }
		},

		async createLoginChallengeByOwnerId(owner_id: string) {
			const credentials = await getCredentialsForOwner(redis, owner_id)
			if (!credentials || credentials.length === 0) {
				return {
					ok: false as const,
					error: 'no_credentials_registered' as const,
				}
			}

			const challenge = randomBytes(32).toString('base64url')

			// Сохраняем challenge в Redis для последующей верификации
			const challengeKey = `webauthn:challenge:${challenge}`
			await redis.setEx(
				challengeKey,
				300, // 5 минут TTL
				JSON.stringify({ owner_id })
			)

			// Преобразуем credentialId в base64url для WebAuthn
			const allowCredentials = credentials.map((cred) =>
				Buffer.from(cred.cred_id, 'utf8').toString('base64url')
			)

			const publicKeyCredentialRequestOptions = {
				challenge: challenge,
				rpId: rpID,
				allowCredentials: allowCredentials.map((id) => ({
					id,
					type: 'public-key',
				})),
				userVerification: 'required',
				timeout: 60_000,
			}

			return {
				ok: true as const,
				publicKeyCredentialRequestOptions,
				allowCredentials: allowCredentials.map((id) => ({
					id,
					type: 'public-key',
				})),
			}
		},

		async createLoginChallengeAuto() {
			// В режиме автоматического выбора браузер сам выберет credential
			const challenge = randomBytes(32).toString('base64url')

			// Сохраняем challenge без owner_id - режим автоматического выбора
			const challengeKey = `webauthn:challenge:${challenge}`
			await redis.setEx(
				challengeKey,
				300, // 5 минут TTL
				JSON.stringify({ auto_mode: true })
			)

			const publicKeyCredentialRequestOptions = {
				challenge: challenge,
				rpId: rpID,
				allowCredentials: [], // Пустой список - браузер выберет сам
				userVerification: 'required',
				timeout: 60_000,
			}

			return {
				ok: true as const,
				publicKeyCredentialRequestOptions,
			}
		},

		async finishLogin(
			sid: string | undefined,
			credential: any,
			challenge: string
		) {
			let session:
				| (WebSessionState & { weblogin_challenge?: string })
				| null = null
			let owner_id: string | undefined

			// Вариант 1: логин по sid (после регистрации)
			if (sid) {
				session = (await loadSession(redis, sid)) as any
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
				owner_id = session.owner_id
			} else {
				// Вариант 2: логин по challenge (без sid)
				const challengeKey = `webauthn:challenge:${challenge}`
				const challengeData = await redis.get(challengeKey)
				if (!challengeData) {
					return {
						ok: false as const,
						error: 'challenge_invalid' as const,
					}
				}
				try {
					const parsed = JSON.parse(challengeData) as {
						owner_id?: string
						auto_mode?: boolean
					}
					if (parsed.auto_mode) {
						// Режим автоматического выбора - достаём owner_id из credential'а
						const credId: string | undefined = credential?.id
						if (!credId) {
							return {
								ok: false as const,
								error: 'assertion_failed' as const,
							}
						}
						const recordRaw = await redis.get(
							webAuthnCredKey(credId)
						)
						if (!recordRaw) {
							return {
								ok: false as const,
								error: 'assertion_failed' as const,
							}
						}
						try {
							const rec = JSON.parse(
								recordRaw
							) as WebAuthnCredentialRecord
							owner_id = rec.owner_id
						} catch {
							return {
								ok: false as const,
								error: 'assertion_failed' as const,
							}
						}
					} else {
						owner_id = parsed.owner_id
					}
					await redis.del(challengeKey) // delete one-time challenge
				} catch {
					return {
						ok: false as const,
						error: 'challenge_invalid' as const,
					}
				}
			}

			// TODO: реальная проверка assertion через @simplewebauthn/server.
			try {
				const credId: string | undefined = credential?.id
				if (!credId) {
					return {
						ok: false as const,
						error: 'assertion_failed' as const,
					}
				}

				// Если у нас есть session и привязанный ключ, проверяем, что это тот же ключ
				if (
					session &&
					session.browser_key_id &&
					credId !== session.browser_key_id
				) {
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

				const exp = nowSeconds() + defaultSessionTtlSeconds
				const session_jwt = await signWebSessionJwt({
					secret: sessionJwtSecret,
					exp,
					claims: {
						sid,
						owner_id,
						// Include hub/subnet hints so the frontend can reliably route to `/hubs/<hub_id>`
						// even when localStorage is empty or has been cleared.
						hub_id: session?.hub_id,
						subnet_id: session?.subnet_id,
						browser_key_id: credId,
						stage: 'AUTH',
					},
				})

				// Если есть session, обновляем её
				if (session) {
					const loginSession: WebSessionState = {
						...session,
						stage: 'AUTH',
						exp,
					}
					delete (loginSession as any).weblogin_challenge
					await saveSession(redis, loginSession)
				}

				// Сохраняем session_jwt в Redis
				await redis.setEx(
					`session:jwt:${session_jwt}`,
					defaultSessionTtlSeconds,
					JSON.stringify({
						sid,
						owner_id,
						browser_key_id: credId,
					})
				)

				return {
					ok: true as const,
					session_jwt,
					browser_key_id: credId,
					owner_id,
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

export function extractUserCode(body: unknown): string | null {
	if (!body || typeof body !== 'object') return null
	const b = body as any
	const user_code = (b.user_code ?? b.device_code ?? b.code) as unknown
	if (typeof user_code !== 'string' || !user_code.trim()) return null
	return user_code.trim()
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

	// POST /v1/owner/login/verify { user_code, sid }
	app.post('/v1/owner1/login/verify', async (req, res) => {
		try {
			const sid = extractSid(req.body)
			const code = extractUserCode(req.body)
			if (!sid) {
				return respondError(req, res, 400, 'missing_field', {
					field: 'sid',
				})
			}
			if (!code) {
				return respondError(req, res, 400, 'missing_field', {
					field: 'user_code',
				})
			}
			const result = await service.verifyDeviceCodeLogin(code, sid)
			if (!result.ok) {
				const status = result.error === 'invalid_user_code' ? 400 : 400
				const errCode = result.error ?? 'invalid_user_code'
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
						? 'invalid_user_code'
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
					session_not_found: 'invalid_user_code',
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
						? 'registration_required'
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

	// POST /v1/owner/webauthn/login/challenge-by-owner { }
	app.post(
		'/v1/owner1/webauthn/login/challenge-by-owner',
		async (req, res) => {
			try {
				// Без параметров - режим автоматического выбора
				const result = await service.createLoginChallengeAuto()
				if (!result.ok) {
					return respondError(req, res, 400, 'invalid_request')
				}
				res.json({
					publicKeyCredentialRequestOptions:
						result.publicKeyCredentialRequestOptions,
				})
			} catch (error) {
				console.error('webauthn.login.challenge-by-owner error', error)
				respondError(req, res, 500, 'internal_error')
			}
		}
	)

	// POST /v1/owner/webauthn/login/finish { sid, credential, challenge }
	app.post('/v1/owner1/webauthn/login/finish', async (req, res) => {
		try {
			const sid = extractSid(req.body)
			const credential = (req.body as any)?.credential
			const challenge = (req.body as any)?.challenge
			if (!credential) {
				return respondError(req, res, 400, 'missing_field', {
					field: 'credential',
				})
			}
			if (!challenge) {
				return respondError(req, res, 400, 'missing_field', {
					field: 'challenge',
				})
			}
			// sid может быть undefined для логина без user_code
			const result = await service.finishLogin(
				sid || undefined,
				credential,
				challenge
			)
			if (!result.ok) {
				return respondError(req, res, 400, 'invalid_request')
			}
			res.json({
				session_jwt: result.session_jwt,
				browser_key_id: result.browser_key_id,
				owner_id: result.owner_id,
			})
		} catch (error) {
			console.error('webauthn.login.finish error', error)
			respondError(req, res, 500, 'internal_error')
		}
	})
}
