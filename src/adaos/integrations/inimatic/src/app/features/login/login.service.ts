import { Injectable } from '@angular/core'
import { HttpErrorResponse } from '@angular/common/http'
import { RootClient } from '../../core/adaos/adaos-client.service'
import { Observable, from, throwError, firstValueFrom } from 'rxjs'
import { catchError } from 'rxjs/operators'
import { startRegistration, startAuthentication } from '@simplewebauthn/browser'

/**
 * Результат успешного WebAuthn-входа.
 * sessionJwt — токен сессии владельца (используется для сокета /owner и последующих запросов).
 */
export interface LoginResult {
	sessionJwt: string
	browserKeyId: string
	sid?: string
	ownerId?: string
}

interface VerifyDeviceCodeResponse {
	ok: boolean
	owner_id: string
	subnet_id?: string
	hub_id?: string
}

interface RegistrationChallengeResponse {
	publicKeyCredentialCreationOptions: any
}

interface RegistrationFinishResponse {
	browser_key_id: string
}

interface LoginChallengeResponse {
	publicKeyCredentialRequestOptions: any
}

interface LoginFinishResponse {
	session_jwt: string
	browser_key_id: string
	owner_id?: string
}

@Injectable({
	providedIn: 'root',
})
export class LoginService {
	private readonly sidKey = 'adaos_web_sid'
	private readonly sessionKey = 'adaos_web_session_jwt'
	private readonly hubIdKey = 'adaos_hub_id'
	private sid: string | null = null
	private sessionJwt: string | null = null

	constructor(private root: RootClient) {
		this.sid = this.restoreSid()
		this.sessionJwt = this.restoreSessionJwt()
	}

	/**
	 * Выполнить полный цикл логина по user code + WebAuthn (регистрация).
	 *
	 * 1) /v1/owner/login/verify — связываем sid и owner.
	 * 2) Регистрируем WebAuthn credential.
	 * 3) Автоматически логинимся через зарегистрированный credential.
	 */
	register(userCode: string): Observable<LoginResult> {
		return from(this.registerInternal(userCode)).pipe(
			catchError((error: HttpErrorResponse) => {
				return throwError(() => error)
			})
		)
	}

	/**
	 * Выполнить логин по WebAuthn без каких-либо параметров.
	 * Браузер автоматически выберет подходящий credential из сохраненных.
	 */
	login(): Observable<LoginResult> {
		return from(this.loginInternal()).pipe(
			catchError((error: HttpErrorResponse) => {
				return throwError(() => error)
			})
		)
	}

	getSessionJwt(): string | null {
		return this.sessionJwt
	}

	getSid(): string | null {
		return this.sid
	}

	// ----------------- Внутренняя логика -----------------

	private async registerInternal(userCode: string): Promise<LoginResult> {
		const sid = this.ensureSid()

		// 1. Связываем sid с owner через введённый код
		await this.verifyUserCode(userCode, sid)

		// 2. Выполняем WebAuthn регистрацию
		await this.performWebAuthnRegistration(sid)

		// 3. Выполняем WebAuthn логин сразу после регистрации
		return await this.performWebAuthnLoginWithSid(sid)
	}

	private async loginInternal(): Promise<LoginResult> {
		// Запрашиваем challenge без указания owner_id
		// Backend вернет пустой список allowCredentials, браузер выберет автоматически
		return await this.performWebAuthnLoginAuto()
	}

	/**
	 * Убедиться, что у браузера есть sid (web session id).
	 * В простом варианте генерируем UUID и кэшируем в localStorage.
	 */
	private ensureSid(): string {
		if (this.sid) {
			return this.sid
		}
		const existing = this.restoreSid()
		if (existing) {
			this.sid = existing
			return existing
		}
		const newSid = (crypto as any)?.randomUUID
			? (crypto as any).randomUUID()
			: this.randomSidFallback()
		this.sid = newSid
		localStorage.setItem(this.sidKey, newSid)
		return newSid
	}

	private restoreSid(): string | null {
		try {
			const stored = localStorage.getItem(this.sidKey)
			return stored && stored.trim() ? stored.trim() : null
		} catch {
			return null
		}
	}

	private restoreSessionJwt(): string | null {
		try {
			const stored = localStorage.getItem(this.sessionKey)
			const tok = stored && stored.trim() ? stored.trim() : null
			// Root-proxy now expects signed JWTs; legacy opaque `sess_*` can't be validated statelessly.
			if (tok && !tok.includes('.')) {
				try {
					localStorage.removeItem(this.sessionKey)
				} catch {}
				return null
			}
			return tok
		} catch {
			return null
		}
	}

	private randomSidFallback(): string {
		return `sid_${Math.random()
			.toString(16)
			.slice(2)}_${Date.now().toString(16)}`
	}

	private async verifyUserCode(userCode: string, sid: string): Promise<void> {
		await firstValueFrom(
			this.root.post<VerifyDeviceCodeResponse>(
				'/v1/owner1/login/verify',
				{
					sid,
					user_code: userCode,
				}
			)
		)
	}

	private async performWebAuthnRegistration(sid: string): Promise<void> {
		this.ensureWebAuthnSupported()

		// 1) запрос challenge
		const { publicKeyCredentialCreationOptions } = await firstValueFrom(
			this.root.post<RegistrationChallengeResponse>(
				'/v1/owner1/webauthn/registration/challenge',
				{ sid }
			)
		)

		// 2) локальная регистрация через WebAuthn API
		const credential = await startRegistration(
			publicKeyCredentialCreationOptions
		)

		// 3) отправка результата на backend
		await firstValueFrom(
			this.root.post<RegistrationFinishResponse>(
				'/v1/owner1/webauthn/registration/finish',
				{
					sid,
					credential,
				}
			)
		)
	}

	private async performWebAuthnLogin(sid: string): Promise<LoginResult> {
		this.ensureWebAuthnSupported()

		// 1) запрос challenge для логина
		const { publicKeyCredentialRequestOptions, challenge } =
			await firstValueFrom(
				this.root.post<LoginChallengeResponse & { challenge?: string }>(
					'/v1/owner1/webauthn/login/challenge',
					{ sid }
				)
			)

		// 2) локальная аутентификация
		const assertion = await startAuthentication(
			publicKeyCredentialRequestOptions
		)

		// 3) отправка assertion на backend
		const finish = await firstValueFrom(
			this.root.post<LoginFinishResponse>(
				'/v1/owner1/webauthn/login/finish',
				{
					sid,
					credential: assertion,
					challenge:
						challenge ||
						publicKeyCredentialRequestOptions.challenge,
				}
			)
		)

		this.sessionJwt = finish.session_jwt
		try {
			localStorage.setItem(this.sessionKey, finish.session_jwt)
		} catch {
			// ignore storage errors
		}
		try {
			if (finish.owner_id) localStorage.setItem(this.hubIdKey, finish.owner_id)
		} catch {}

		return {
			sessionJwt: finish.session_jwt,
			browserKeyId: finish.browser_key_id,
			sid,
			ownerId: finish.owner_id,
		}
	}

	private async performWebAuthnLoginWithSid(
		sid: string
	): Promise<LoginResult> {
		this.ensureWebAuthnSupported()

		// 1) запрос challenge для логина
		const challengeResponse = await firstValueFrom(
			this.root.post<LoginChallengeResponse & { challenge?: string }>(
				'/v1/owner1/webauthn/login/challenge',
				{ sid }
			)
		)

		// 2) локальная аутентификация
		const assertion = await startAuthentication(
			challengeResponse.publicKeyCredentialRequestOptions
		)

		// 3) отправка assertion на backend
		const finish = await firstValueFrom(
			this.root.post<LoginFinishResponse>(
				'/v1/owner1/webauthn/login/finish',
				{
					sid,
					credential: assertion,
					challenge:
						challengeResponse.challenge ||
						challengeResponse.publicKeyCredentialRequestOptions
							.challenge,
				}
			)
		)

		this.sessionJwt = finish.session_jwt
		try {
			localStorage.setItem(this.sessionKey, finish.session_jwt)
		} catch {
			// ignore storage errors
		}
		try {
			if (finish.owner_id) localStorage.setItem(this.hubIdKey, finish.owner_id)
		} catch {}

		return {
			sessionJwt: finish.session_jwt,
			browserKeyId: finish.browser_key_id,
			sid,
			ownerId: finish.owner_id,
		}
	}

	private async performWebAuthnLoginAuto(): Promise<LoginResult> {
		this.ensureWebAuthnSupported()

		// 1) запрос challenge без owner_id - браузер сам выберет credential
		const challengeResponse = await firstValueFrom(
			this.root.post<LoginChallengeResponse & { challenge?: string }>(
				'/v1/owner1/webauthn/login/challenge-by-owner',
				{}
			)
		)

		// 2) локальная аутентификация - браузер выберет подходящий credential
		const assertion = await startAuthentication(
			challengeResponse.publicKeyCredentialRequestOptions
		)

		// 3) отправка assertion на backend без sid
		const finish = await firstValueFrom(
			this.root.post<LoginFinishResponse>(
				'/v1/owner1/webauthn/login/finish',
				{
					credential: assertion,
					challenge:
						challengeResponse.challenge ||
						challengeResponse.publicKeyCredentialRequestOptions
							.challenge,
				}
			)
		)

		this.sessionJwt = finish.session_jwt
		try {
			localStorage.setItem(this.sessionKey, finish.session_jwt)
		} catch {
			// ignore storage errors
		}
		try {
			if (finish.owner_id) localStorage.setItem(this.hubIdKey, finish.owner_id)
		} catch {}

		return {
			sessionJwt: finish.session_jwt,
			browserKeyId: finish.browser_key_id,
			ownerId: finish.owner_id,
		}
	}

	private ensureWebAuthnSupported(): void {
		if (typeof window === 'undefined') {
			throw new Error('WebAuthn is not available in this environment')
		}
		if (!('PublicKeyCredential' in window)) {
			throw new Error('This browser does not support WebAuthn')
		}
	}
}
