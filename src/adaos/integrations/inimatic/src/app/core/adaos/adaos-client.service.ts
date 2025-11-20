import { HttpClient, HttpHeaders } from '@angular/common/http'
import { Injectable } from '@angular/core'

export type AdaosEvent = { type: string; [k: string]: any }
export interface AdaosConfig {
	baseUrl: string
	token?: string | null
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

	constructor(private http: HttpClient) {
		this.cfg = {
			baseUrl: (window as any).__ADAOS_BASE__ ?? 'http://127.0.0.1:8777',
			token: (window as any).__ADAOS_TOKEN__ ?? null,
		}
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

	// �����⭠� ᪫���� ��� new URL - ࠡ�⠥� � � ��᮫�⭮�, � � �⭮�⥫쭮� �����
	private abs(path: string) {
		const base = this.cfg.baseUrl.replace(/\/$/, '')
		const rel = path.startsWith('/') ? path : `/${path}`
		return `${base}${rel}`
	}
	private h() {
		return this.cfg.token
			? new HttpHeaders({ 'X-AdaOS-Token': this.cfg.token })
			: undefined
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
		this.ensureEventsSocket()
			.then((ws) => {
				ws.send(JSON.stringify({ type: 'subscribe', topics }))
			})
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
		ws.send(JSON.stringify(envelope))
		return ack
	}

	say(text: string) {
		return this.post('/api/say', { text })
	}
	callSkill<T = any>(skill: string, method: string, body?: any) {
		return this.post<T>(`/api/skills/${skill}/${method}`, body ?? {})
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
