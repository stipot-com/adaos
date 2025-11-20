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
	sid: string
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
}

@Injectable({
	providedIn: 'root',
})
export class LoginService {
	private readonly sidKey = 'adaos_web_sid'
	private readonly sessionKey = 'adaos_web_session_jwt'
	private sid: string | null = null
	private sessionJwt: string | null = null

	constructor(private root: RootClient) {
		this.sid = this.restoreSid()
		this.sessionJwt = this.restoreSessionJwt()
	}

	/**
	 * Выполнить полный цикл логина по device code + WebAuthn.
	 *
	 * 1) /v1/owner/login/verify — связываем sid и owner.
	 * 2) Пытаемся WebAuthn login.
	 * 3) Если нужна регистрация — выполняем WebAuthn регистрацию и после неё логин.
	 */
	login(deviceCode: string): Observable<LoginResult> {
		return from(this.loginInternal(deviceCode)).pipe(
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

	private async loginInternal(deviceCode: string): Promise<LoginResult> {
		const sid = this.ensureSid()

		// 1. Связываем sid с owner через введённый код
		await this.verifyDeviceCode(deviceCode, sid)

		// 2. Пытаемся выполнить WebAuthn login; если потребуется регистрация — сделаем её и повторим
		try {
			return await this.performWebAuthnLogin(sid)
		} catch (error: any) {
			if (this.isRegistrationRequiredError(error)) {
				await this.performWebAuthnRegistration(sid)
				return await this.performWebAuthnLogin(sid)
			}
			throw error
		}
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
			return stored && stored.trim() ? stored.trim() : null
		} catch {
			return null
		}
	}

	private randomSidFallback(): string {
		return `sid_${Math.random()
			.toString(16)
			.slice(2)}_${Date.now().toString(16)}`
	}

	private async verifyDeviceCode(
		deviceCode: string,
		sid: string
	): Promise<void> {
		await firstValueFrom(
			this.root.post<VerifyDeviceCodeResponse>(
				'/v1/owner1/login/verify',
				{
					sid,
					device_code: deviceCode,
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
		const { publicKeyCredentialRequestOptions } = await firstValueFrom(
			this.root.post<LoginChallengeResponse>(
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
				}
			)
		)

		this.sessionJwt = finish.session_jwt
		try {
			localStorage.setItem(this.sessionKey, finish.session_jwt)
		} catch {
			// ignore storage errors
		}

		return {
			sessionJwt: finish.session_jwt,
			browserKeyId: finish.browser_key_id,
			sid,
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

	/**
	 * Проверка: ошибка от /webauthn/login/challenge, означающая, что браузеру нужно сначала зарегистрировать ключ.
	 */
	private isRegistrationRequiredError(error: unknown): boolean {
		if (!(error instanceof HttpErrorResponse)) return false
		if (error.status !== 400) return false
		const payload = error.error as any
		const code =
			typeof payload?.code === 'string'
				? payload.code
				: typeof payload?.error === 'string'
				? payload.error
				: ''
		return code === 'registration_required'
	}
}
