import { HttpClient, HttpHeaders } from '@angular/common/http'
import { Injectable } from '@angular/core'
import { BehaviorSubject } from 'rxjs'
import { map } from 'rxjs/operators'
import { HubMemberChannelsService } from './hub-member-channels.service'

export type AdaosEvent = { type: string; [k: string]: any }
export interface AdaosConfig {
	baseUrl: string
	token?: string | null
	authKind?: 'adaos-token' | 'bearer'
}

export interface SubnetRegisterRequest {
	csr_pem: string
	fingerprint: string
	owner_token: string
	hints?: any
	idempotencyKey?: string
}

export interface SubnetRegisterData {
	subnet_id: string
	hub_device_id: string
	cert_pem: string
}

export interface SubnetRegisterResponse {
	data: SubnetRegisterData | null
	event_id: string
	server_time_utc: string
}

export type AdaosEventsConnectionState =
	| 'disconnected'
	| 'connecting'
	| 'connected'

const ROOT_BASE = (() => {
	const value = (window as any).__ADAOS_ROOT_BASE__ ?? 'http://127.0.0.1:3030'
	return typeof value === 'string'
		? value.replace(/\/$/, '')
		: 'http://127.0.0.1:3030'
})()

function isLoopbackHost(host: string): boolean {
	const normalized = String(host || '').trim().toLowerCase()
	return normalized === 'localhost' || normalized === '127.0.0.1' || normalized === '::1'
}

function isLoopbackUrl(url: string): boolean {
	try {
		return isLoopbackHost(new URL(url).hostname)
	} catch {
		return false
	}
}

function allowLoopbackHub(): boolean {
	try {
		const url = new URL(window.location.href)
		const q = (url.searchParams.get('try_local_hub') || '').trim().toLowerCase()
		if (q === '0' || q === 'false') return false
		if (q === '1' || q === 'true') return true
	} catch {}
	try {
		const v = (localStorage.getItem('adaos_try_local_hub') || '').trim()
		if (v === '0') return false
		if (v === '1') return true
	} catch {}
	try {
		return isLoopbackHost(String(window.location.hostname || ''))
	} catch {
		return false
	}
}

function allowReservedLocalHub(): boolean {
	try {
		const url = new URL(window.location.href)
		const q = (url.searchParams.get('try_local_hub') || '').trim().toLowerCase()
		if (q === '0' || q === 'false') return false
		if (q === '1' || q === 'true') return true
	} catch {}
	try {
		const v = (localStorage.getItem('adaos_try_local_hub') || '').trim()
		if (v === '0') return false
		if (v === '1') return true
	} catch {}
	return true
}

function isReservedLocalHubUrl(url: string): boolean {
	try {
		const parsed = new URL(url)
		return (
			isLoopbackHost(parsed.hostname) &&
			(parsed.port || (parsed.protocol === 'https:' ? '443' : '80')) === '8777'
		)
	} catch {
		return false
	}
}

function defaultHubBaseUrl(): string {
	return allowReservedLocalHub() ? 'http://127.0.0.1:8777' : ROOT_BASE
}

function decodeStoredJwtPayload(token: string): any | null {
	try {
		const parts = token.split('.')
		if (parts.length < 2) return null
		const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
		const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4)
		return JSON.parse(atob(padded))
	} catch {
		return null
	}
}

function getStoredBoundSubnetSession(): {
	baseUrl: string
	sessionJwt: string
} | null {
	try {
		const sessionJwt = (localStorage.getItem('adaos_web_session_jwt') || '').trim()
		if (!sessionJwt || !sessionJwt.includes('.')) return null
		const payload = decodeStoredJwtPayload(sessionJwt)
		const exp = typeof payload?.exp === 'number' ? payload.exp : undefined
		if (!exp) return null
		const now = Math.floor(Date.now() / 1000)
		if (exp <= now + 15) return null
		const hubId =
			(localStorage.getItem('adaos_hub_id') || '').trim() ||
			String(payload?.hub_id || payload?.subnet_id || payload?.owner_id || '').trim()
		if (!hubId) return null
		return {
			baseUrl: `https://api.inimatic.com/hubs/${hubId}`,
			sessionJwt,
		}
	} catch {
		return null
	}
}

function rootAbs(path: string) {
	const rel = path.startsWith('/') ? path : `/${path}`
	return `${ROOT_BASE}${rel}`
}

/**
 * HTTP-клиент для локального AdaOS hub (по умолчанию http://127.0.0.1:8777).
 * Используется для вызова API хаба и websocket-событий.
 */
@Injectable({ providedIn: 'root' })
export class AdaosClient {
	private cfg: AdaosConfig
	readonly eventsConnectionState$ =
		new BehaviorSubject<AdaosEventsConnectionState>('disconnected')

	constructor(
		private http: HttpClient,
		private channels: HubMemberChannelsService,
	) {
		const boundSubnet = (() => {
			try {
				return getStoredBoundSubnetSession()
			} catch {
				return null
			}
		})()
		const lsBase = (() => {
			try {
				const persisted = (localStorage.getItem('adaos_hub_base') || '').trim()
				if (!persisted) return null
				// Persisted local hub base is an explicit user/browser choice.
				// Keep honoring it even on a public origin so non-default local
				// ports such as 8778 survive reloads.
				return persisted
			} catch {
				return null
			}
		})()
		const lsToken = (() => {
			try {
				const v = (localStorage.getItem('adaos_hub_token') || '').trim()
				return v ? v : null
			} catch {
				return null
			}
		})()
		this.cfg = {
			baseUrl:
				(window as any).__ADAOS_BASE__ ??
				boundSubnet?.baseUrl ??
				(lsBase && lsBase.trim() ? lsBase.trim() : null) ??
				defaultHubBaseUrl(),
			token:
				(window as any).__ADAOS_TOKEN__ ??
				boundSubnet?.sessionJwt ??
				lsToken ??
				null,
			authKind: boundSubnet?.sessionJwt ? 'bearer' : 'adaos-token',
		}
		this.channels.snapshot$.subscribe((snapshot) => {
			const sessionState = snapshot.controlPlane.sessionState
			this.eventsConnectionState$.next(
				sessionState === 'connected'
					? 'connected'
					: sessionState === 'connecting'
						? 'connecting'
						: 'disconnected',
			)
		})
	}

	/**
	 * Ensure member transport is ready for app-level command/sync flows.
	 * The WS control path is always established; direct paths are negotiated
	 * only when explicitly allowed.
	 */
	async prepareMemberTransport({
		topics = [],
	}: {
		topics?: string[]
	} = {}): Promise<{ ws: WebSocket; direct: boolean }> {
		const ws = await this.connect(topics)
		const sendCmd = (kind: string, payload: Record<string, any>) =>
			this.sendEventsCommand(kind, payload, 8000)

		const ok = await this.channels.prepareDirectPaths(ws, sendCmd, {
			onEventsMessage: (data: string) => {
				this.channels.handleIncomingControlMessage(data)
			},
			remoteProxy: this.getBaseUrl().includes('/hubs/'),
		})
		return { ws, direct: ok }
	}

	getBaseUrl() {
		return this.cfg.baseUrl
	}

	setBase(url: string) {
		this.cfg.baseUrl = url.replace(/\/$/, '')
	}
	setToken(token: string | null) {
		this.cfg.token = token
	}
	setAuthBearer(token: string | null) {
		this.cfg.token = token
		this.cfg.authKind = 'bearer'
	}
	setAuthAdaosToken(token: string | null) {
		this.cfg.token = token
		this.cfg.authKind = 'adaos-token'
	}
	getToken(): string | null | undefined {
		return this.cfg.token
	}

	getAuthHeaders(): Record<string, string> {
		if (!this.cfg.token) return {}
		if (this.cfg.authKind === 'bearer') {
			return { Authorization: `Bearer ${this.cfg.token}` }
		}
		return { 'X-AdaOS-Token': String(this.cfg.token) }
	}

	// �����⭠� ᪫���� ��� new URL - ࠡ�⠥� � � ��᮫�⭮�, � � �⭮�⥫쭮� �����
	private abs(path: string) {
		const base = this.cfg.baseUrl.replace(/\/$/, '')
		const rel = path.startsWith('/') ? path : `/${path}`
		return `${base}${rel}`
	}
	private h() {
		if (!this.cfg.token) return undefined
		if (this.cfg.authKind === 'bearer') {
			return new HttpHeaders({ Authorization: `Bearer ${this.cfg.token}` })
		}
		return new HttpHeaders({ 'X-AdaOS-Token': this.cfg.token })
	}

	get<T>(path: string) {
		return this.http.get<T>(this.abs(path), { headers: this.h() })
	}
	post<T>(path: string, body?: any) {
		return this.http.post<T>(this.abs(path), body ?? {}, {
			headers: this.h(),
		})
	}

	private eventsUrl(): string {
		const wsUrl = this.abs('/ws').replace(/^http/, 'ws')
		const u = new URL(wsUrl)
		if (this.cfg.token) u.searchParams.set('token', this.cfg.token)
		return u.toString()
	}

	async connect(topics: string[] = []): Promise<WebSocket> {
		const ws = await this.channels.ensureControlSession(this.eventsUrl())
		if (topics.length) {
			this.subscribe(topics)
		}
		return ws
	}

	getEventsSocket(): WebSocket | undefined {
		return this.channels.getControlSession()
	}

	subscribe(topics: string[]) {
		if (!topics.length) return
		const added = this.channels.registerControlSubscriptions(topics)
		if (!added.length) return
		const ws = this.channels.getControlSession()
		if (ws && ws.readyState === WebSocket.OPEN) {
			this.channels.sendControlSubscriptions(ws, added)
			return
		}
		this.channels.ensureControlSession(this.eventsUrl()).catch(() => {})
	}

	async sendEventsCommand(
		kind: string,
		payload: Record<string, any>,
		timeoutMs = 5000
	): Promise<any> {
		const ws = await this.channels.ensureControlSession(this.eventsUrl())
		const command = this.channels.createPendingControlCommand(
			kind,
			payload,
			timeoutMs,
		)
		try {
			this.channels.sendCommandEnvelope(ws, kind, command.json)
		} catch (err) {
			this.channels.failPendingControlCommand(command.id, err, 'error')
		}
		return command.ack
	}

	say(text: string) {
		return this.post('/api/say', { text })
	}

	/**
	 * Expose current webspace id to higher-level services so that
	 * callHost/actions can stamp events with an explicit webspace_id.
	 */
	getCurrentWebspaceId(): string | undefined {
		try {
			// YDocService persists preferred webspace under this key.
			const key = 'adaos_webspace_id'
			const value = localStorage.getItem(key)
			return value || undefined
		} catch {
			return undefined
		}
	}
	callSkill<T = any>(skill: string, method: string, body?: any) {
		const tool = `${skill}:${method}`
		return this.post<{ ok: boolean; result: T }>(`/api/tools/call`, {
			tool,
			arguments: body ?? {},
		}).pipe(map((res) => res.result))
	}
}

/**
 * Минимальный HTTP-клиент для root-сервера (по умолчанию http://127.0.0.1:3030).
 * Используется для вызовов owner/WebAuthn и регистрационных эндпоинтов.
 */
@Injectable({ providedIn: 'root' })
export class RootClient {
	constructor(private http: HttpClient) {}

	get<T>(path: string) {
		return this.http.get<T>(`https://api.inimatic.com${path}`)
	}

	post<T>(path: string, body?: any) {
		return this.http.post<T>(`https://api.inimatic.com${path}`, body ?? {})
	}
}

export async function subnetRegister(
	req: SubnetRegisterRequest
): Promise<SubnetRegisterResponse> {
	const headers: Record<string, string> = {
		'Content-Type': 'application/json',
	}
	if (req.idempotencyKey) headers['Idempotency-Key'] = req.idempotencyKey
	const response = await fetch(rootAbs('/v1/subnets/register'), {
		method: 'POST',
		headers,
		body: JSON.stringify({
			csr_pem: req.csr_pem,
			fingerprint: req.fingerprint,
			owner_token: req.owner_token,
			hints: req.hints ?? null,
		}),
	})
	if (!response.ok)
		throw new Error(`subnetRegister failed: ${response.status}`)
	return response.json()
}

export async function subnetRegisterStatus(
	fingerprint: string,
	ownerToken?: string
): Promise<SubnetRegisterResponse> {
	const token = ownerToken ?? (window as any).__ADAOS_ROOT_OWNER_TOKEN__
	if (!token) throw new Error('owner token required')
	const response = await fetch(
		rootAbs(
			`/v1/subnets/register/status?fingerprint=${encodeURIComponent(
				fingerprint
			)}`
		),
		{
			headers: { 'X-Owner-Token': token },
		}
	)
	if (!response.ok)
		throw new Error(`subnetRegisterStatus failed: ${response.status}`)
	return response.json()
}
