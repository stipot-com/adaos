import { HttpClient, HttpHeaders } from '@angular/common/http'
import { Injectable } from '@angular/core'
import { map } from 'rxjs/operators'
import { WebRtcTransportService } from './webrtc-transport.service'

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

const ROOT_BASE = (() => {
	const value = (window as any).__ADAOS_ROOT_BASE__ ?? 'http://127.0.0.1:3030'
	return typeof value === 'string'
		? value.replace(/\/$/, '')
		: 'http://127.0.0.1:3030'
})()

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
	private eventsWs?: WebSocket
	private eventsReady?: Promise<WebSocket>
	private pendingCmds = new Map<
		string,
		{
			resolve: (msg: any) => void
			reject: (err: any) => void
			timeout: any
		}
	>()
	private useWebRtc = false
	private rtcStateSub: { unsubscribe(): void } | null = null

	constructor(
		private http: HttpClient,
		public readonly rtc: WebRtcTransportService
	) {
		const lsBase = (() => {
			try {
				return localStorage.getItem('adaos_hub_base')
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
				(lsBase && lsBase.trim() ? lsBase.trim() : null) ??
				'http://127.0.0.1:8777',
			token: (window as any).__ADAOS_TOKEN__ ?? lsToken ?? null,
			authKind: 'adaos-token',
		}
	}

	/**
	 * Attempt to upgrade the current WS connection to WebRTC DataChannels.
	 * Returns `true` if WebRTC is active, `false` on failure (WS remains).
	 */
	async enableWebRtc(signalingWs: WebSocket): Promise<boolean> {
		// Wire RTC events-channel messages into the same pending-cmd handler
		this.rtc.onEventsMessage = (data: string) => {
			this.onEventsMessage({ data } as MessageEvent)
		}

		const sendCmd = (kind: string, payload: Record<string, any>) =>
			this.sendEventsCommand(kind, payload, 8000)

		const ok = await this.rtc.negotiate(signalingWs, sendCmd)
		this.useWebRtc = ok

		// Clean up previous subscription to avoid memory leaks
		this.rtcStateSub?.unsubscribe()

		// Listen for WebRTC failure → automatic fallback to WS
		this.rtcStateSub = this.rtc.state$.subscribe((st) => {
			if (st === 'failed' && this.useWebRtc) {
				this.useWebRtc = false
			}
		})

		return ok
	}

	isWebRtcActive(): boolean {
		return this.useWebRtc && this.rtc.isConnected()
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

	private resetEventsSocket(reason?: any) {
		if (this.eventsWs) {
			try {
				this.eventsWs.removeEventListener(
					'message',
					this.onEventsMessage
				)
			} catch {}
		}
		this.eventsWs = undefined
		this.eventsReady = undefined
		for (const [, entry] of this.pendingCmds) {
			clearTimeout(entry.timeout)
			entry.reject(reason ?? new Error('events websocket closed'))
		}
		this.pendingCmds.clear()
	}

	private onEventsMessage = (ev: MessageEvent) => {
		try {
			const msg = JSON.parse(ev.data)
			if (msg?.ch === 'events' && msg?.t === 'ack' && msg?.id) {
				const pending = this.pendingCmds.get(String(msg.id))
				if (pending) {
					this.pendingCmds.delete(String(msg.id))
					clearTimeout(pending.timeout)
					pending.resolve(msg)
				}
			}
		} catch {
			// ignore malformed payloads
		}
	}

	private ensureEventsSocket(): Promise<WebSocket> {
		if (this.eventsWs && this.eventsWs.readyState === WebSocket.OPEN) {
			return Promise.resolve(this.eventsWs)
		}
		if (this.eventsReady) {
			return this.eventsReady
		}
		this.eventsReady = new Promise<WebSocket>((resolve, reject) => {
			const ws = new WebSocket(this.eventsUrl())
			this.eventsWs = ws
			const cleanup = () => {
				ws.removeEventListener('open', onOpen)
				ws.removeEventListener('error', onError)
			}
			const onOpen = () => {
				cleanup()
				ws.addEventListener('message', this.onEventsMessage)
				ws.addEventListener('close', () => this.resetEventsSocket())
				resolve(ws)
			}
			const onError = (err: Event) => {
				cleanup()
				this.resetEventsSocket(err)
				reject(err)
			}
			ws.addEventListener('open', onOpen)
			ws.addEventListener('error', onError)
		}).finally(() => {
			this.eventsReady = undefined
		})
		return this.eventsReady
	}

	async connect(topics: string[] = []): Promise<WebSocket> {
		const ws = await this.ensureEventsSocket()
		if (topics.length) {
			this.subscribe(topics)
		}
		return ws
	}

	getEventsSocket(): WebSocket | undefined {
		return this.eventsWs
	}

	subscribe(topics: string[]) {
		if (!topics.length) return
		const msg = JSON.stringify({ type: 'subscribe', topics })
		if (this.useWebRtc && this.rtc.sendEvents(msg)) return
		this.ensureEventsSocket()
			.then((ws) => ws.send(msg))
			.catch(() => {})
	}

	async sendEventsCommand(
		kind: string,
		payload: Record<string, any>,
		timeoutMs = 5000
	): Promise<any> {
		const ws = await this.ensureEventsSocket()
		const cmdId = `${kind}.${Date.now()}.${Math.random()
			.toString(16)
			.slice(2)}`
		const envelope = {
			ch: 'events',
			t: 'cmd',
			id: cmdId,
			kind,
			payload: payload ?? {},
		}
		const ack = new Promise<any>((resolve, reject) => {
			const timeout = setTimeout(() => {
				this.pendingCmds.delete(cmdId)
				reject(new Error(`events command timeout: ${kind}`))
			}, timeoutMs)
			this.pendingCmds.set(cmdId, {
				resolve: (msg: any) => resolve(msg),
				reject: (err: any) => reject(err),
				timeout,
			})
		})
		const json = JSON.stringify(envelope)
		// Signaling commands (rtc.*) must always go through WS even when WebRTC is active.
		const isSignaling = kind.startsWith('rtc.')
		if (!isSignaling && this.useWebRtc && this.rtc.sendEvents(json)) {
			// Sent via WebRTC DataChannel
		} else {
			ws.send(json)
		}
		return ack
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
