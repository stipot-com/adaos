import { Injectable } from '@angular/core'
import { BehaviorSubject } from 'rxjs'
import * as Y from 'yjs'
import { WebsocketProvider } from 'y-websocket'
import { DataChannelProvider } from '../../y/datachannel-provider'
import { WebRtcTransportService } from './webrtc-transport.service'

export type HubMemberSemanticChannelId =
	| 'command'
	| 'event'
	| 'sync'
	| 'presence'
	| 'route'
	| 'media'

export type HubMemberSemanticPath =
	| 'webrtc_data:events'
	| 'ws'
	| 'webrtc_data:yjs'
	| 'yws'

export type HubMemberCommandPolicy =
	| 'member_command'
	| 'control_ws'

export type HubMemberTransportState =
	| 'signaling'
	| 'connecting'
	| 'connected'
	| 'ws'

export type HubMemberDirectPolicySource =
	| 'query'
	| 'storage'
	| 'default'

export type HubMemberDirectNegotiationState =
	| 'idle'
	| 'skipped'
	| 'connected'
	| 'failed'

export type HubMemberDirectRecoveryState =
	| 'idle'
	| 'skipped'
	| 'recovered'
	| 'failed'

export type HubMemberDirectRecoveryMode =
	| 'ice_restart'
	| 'full_renegotiate'

export type HubMemberSyncRecoveryReason =
	| 'first_sync_timeout'
	| 'provider_disconnected'

export type HubMemberSyncRecoveryState =
	| 'idle'
	| 'skipped'
	| 'recreated'
	| 'failed'

export type HubMemberControlSessionState =
	| 'idle'
	| 'connecting'
	| 'connected'
	| 'disconnected'

export type HubMemberControlCommandOutcome =
	| 'ack'
	| 'timeout'
	| 'closed'
	| 'error'

export type HubMemberPathEvidenceState =
	| 'idle'
	| 'connecting'
	| 'connected'
	| 'disconnected'

export type HubMemberChannelHealth =
	| 'ready'
	| 'fallback'
	| 'recovering'
	| 'degraded'
	| 'unavailable'

export type HubMemberMediaPolicyMode = 'out_of_scope'

type ChannelSpec = {
	paths: readonly HubMemberSemanticPath[]
	freezeMs: number
}

type ChannelState = {
	activePath: HubMemberSemanticPath | null
	preferredPath: HubMemberSemanticPath | null
	previousPath: HubMemberSemanticPath | null
	lastSwitchAt: number
	switchTotal: number
}

type PendingControlCommandEntry = {
	kind: string
	resolve: (message: any) => void
	reject: (error: any) => void
	timeout: ReturnType<typeof setTimeout>
}

export type HubMemberChannelSnapshot = {
	updatedAt: number
	directPolicy: {
		enabled: boolean
		source: HubMemberDirectPolicySource
		lastNegotiationAt: number | null
		lastNegotiationState: HubMemberDirectNegotiationState
	}
	directRecovery: {
		eligible: boolean
		lastAttemptAt: number | null
		lastAttemptState: HubMemberDirectRecoveryState
		lastAttemptMode: HubMemberDirectRecoveryMode | null
		attemptCount: number
		failureCount: number
		nextAttemptAt: number | null
	}
	syncRecovery: {
		eligible: boolean
		lastAttemptAt: number | null
		lastAttemptState: HubMemberSyncRecoveryState
		lastReason: HubMemberSyncRecoveryReason | null
		recoveryCount: number
		lastCreatedPath: HubMemberSemanticPath | null
		lastCreatedAt: number | null
	}
	controlPlane: {
		sessionState: HubMemberControlSessionState
		sessionOpenCount: number
		lastConnectedAt: number | null
		lastDisconnectedAt: number | null
		lastCloseReason: string | null
		subscriptionsTracked: number
		pendingCommands: number
		lastCommandKind: string | null
		lastCommandCompletedAt: number | null
		lastCommandOutcome: HubMemberControlCommandOutcome | null
		lastReplayAt: number | null
		lastReplayCount: number
	}
	rtcRuntime: {
		state: ReturnType<WebRtcTransportService['getRecoverySnapshot']>['state']
		iceConnectionState: ReturnType<WebRtcTransportService['getRecoverySnapshot']>['iceConnectionState']
		connectionState: ReturnType<WebRtcTransportService['getRecoverySnapshot']>['connectionState']
		lastConnectedAt: number | null
		lastDisconnectedAt: number | null
		lastFailureAt: number | null
		lastFailureReason: string | null
		canRestartIce: boolean
		canRenegotiate: boolean
	}
	mediaPolicy: {
		mode: HubMemberMediaPolicyMode
		reason: string
	}
	pathEvidence: Record<
		HubMemberSemanticPath,
		{
			state: HubMemberPathEvidenceState
		}
	>
	channels: Record<
		HubMemberSemanticChannelId,
		{
			activePath: HubMemberSemanticPath | null
			preferredPath: HubMemberSemanticPath | null
			availablePaths: HubMemberSemanticPath[]
			freezeRemainingMs: number
			switchTotal: number
			activeReady: boolean
			health: HubMemberChannelHealth
		}
	>
}

const CHANNEL_SPECS: Record<HubMemberSemanticChannelId, ChannelSpec> = {
	command: {
		paths: ['webrtc_data:events', 'ws'],
		freezeMs: 10_000,
	},
	event: {
		paths: ['webrtc_data:events', 'ws'],
		freezeMs: 10_000,
	},
	sync: {
		paths: ['webrtc_data:yjs', 'yws'],
		freezeMs: 15_000,
	},
	presence: {
		paths: ['webrtc_data:events', 'ws'],
		freezeMs: 5_000,
	},
	route: {
		paths: ['ws'],
		freezeMs: 0,
	},
	media: {
		paths: [],
		freezeMs: 0,
	},
}

@Injectable({ providedIn: 'root' })
export class HubMemberChannelsService {
	private static readonly DIRECT_RECOVERY_HEALTH_CHECK_MS = 30_000
	private static readonly DIRECT_RECOVERY_DISCONNECT_GRACE_MS = 3_000
	private static readonly DIRECT_RECOVERY_DEBOUNCE_MS = 1_000
	private static readonly DIRECT_RECOVERY_BACKOFF_BASE_MS = 15_000
	private static readonly DIRECT_RECOVERY_BACKOFF_MAX_MS = 5 * 60_000
	private static readonly DIRECT_RECOVERY_ICE_RESTART_LIMIT = 3
	private static readonly SYNC_RECOVERY_DEBOUNCE_MS = 1_500

	private readonly states = new Map<HubMemberSemanticChannelId, ChannelState>()
	private readonly controlSubscriptions = new Set<string>()
	private readonly pendingControlCommands = new Map<
		string,
		PendingControlCommandEntry
	>()
	private directSignalCommand:
		| ((kind: string, payload: Record<string, any>) => Promise<any>)
		| null = null
	private controlSessionSocket?: WebSocket
	private controlSessionReady?: Promise<WebSocket>
	private runtimeInitialized = false
	private visibilityTrackingInstalled = false
	private isPageVisible = true
	private directRecoveryDebounce: ReturnType<typeof setTimeout> | null = null
	private directRecoveryInFlight?: Promise<boolean>
	private directRemoteProxyEligible = false
	private lastDirectNegotiationAt: number | null = null
	private lastDirectNegotiationState: HubMemberDirectNegotiationState = 'idle'
	private lastDirectRecoveryAt: number | null = null
	private lastDirectRecoveryState: HubMemberDirectRecoveryState = 'idle'
	private lastDirectRecoveryMode: HubMemberDirectRecoveryMode | null = null
	private directRecoveryAttemptCount = 0
	private directRecoveryIceRestartCount = 0
	private directRecoveryFailureCount = 0
	private nextDirectRecoveryAt: number | null = null
	private lastSyncProviderCreateAt: number | null = null
	private lastSyncProviderPath: HubMemberSemanticPath | null = null
	private lastSyncRecoveryAt: number | null = null
	private lastSyncRecoveryState: HubMemberSyncRecoveryState = 'idle'
	private lastSyncRecoveryReason: HubMemberSyncRecoveryReason | null = null
	private syncRecoveryCount = 0
	private controlSessionState: HubMemberControlSessionState = 'idle'
	private controlSessionOpenCount = 0
	private lastControlConnectedAt: number | null = null
	private lastControlDisconnectedAt: number | null = null
	private lastControlCloseReason: string | null = null
	private lastControlCommandKind: string | null = null
	private lastControlCommandCompletedAt: number | null = null
	private lastControlCommandOutcome: HubMemberControlCommandOutcome | null = null
	private lastControlReplayAt: number | null = null
	private lastControlReplayCount = 0
	private wsState: HubMemberPathEvidenceState = 'idle'
	private syncPathEvidence: {
		path: 'webrtc_data:yjs' | 'yws' | null
		state: HubMemberPathEvidenceState
	} = {
		path: null,
		state: 'idle',
	}
	readonly snapshot$ = new BehaviorSubject<HubMemberChannelSnapshot>(
		this.buildSnapshot(),
	)
	readonly transportState$ = new BehaviorSubject<HubMemberTransportState>(
		this.buildTransportState(this.snapshot$.value),
	)

	constructor(private rtc: WebRtcTransportService) {
		this.rtc.onEventsMessage = (data: string) => {
			this.handleIncomingControlMessage(data)
		}
		this.rtc.onLocalIceCandidate = (candidate) => {
			void this.sendRtcSignal('rtc.ice', { candidate })
		}
		this.rtc.state$.subscribe((state) => {
			if (
				(state === 'disconnected' || state === 'failed') &&
				this.wsState === 'connected' &&
				this.isPageVisible
			) {
				this.scheduleDirectRecovery()
			}
			this.publishSnapshot()
		})
	}

	reportWsState(state: 'disconnected' | 'connecting' | 'connected'): void {
		this.wsState = state
		if (state === 'connected' && this.isPageVisible) {
			this.scheduleDirectRecovery()
		}
		this.publishSnapshot()
	}

	reportControlSessionConnecting(): void {
		this.controlSessionState = 'connecting'
		this.publishSnapshot()
	}

	async ensureControlSession(url: string): Promise<WebSocket> {
		if (
			this.controlSessionSocket &&
			this.controlSessionSocket.readyState === WebSocket.OPEN
		) {
			return this.controlSessionSocket
		}
		if (this.controlSessionReady) {
			return this.controlSessionReady
		}
		this.reportWsState('connecting')
		this.reportControlSessionConnecting()
		this.controlSessionReady = new Promise<WebSocket>((resolve, reject) => {
			const ws = new WebSocket(url)
			this.controlSessionSocket = ws
			const cleanup = () => {
				ws.removeEventListener('open', onOpen)
				ws.removeEventListener('error', onError)
			}
			const onOpen = () => {
				cleanup()
				this.reportWsState('connected')
				ws.addEventListener('message', this.onControlSessionMessage)
				ws.addEventListener('close', this.onControlSessionClose)
				this.onControlWsOpen(ws)
				resolve(ws)
			}
			const onError = (err: Event) => {
				cleanup()
				this.resetControlSession(err)
				reject(err)
			}
			ws.addEventListener('open', onOpen)
			ws.addEventListener('error', onError)
		}).finally(() => {
			this.controlSessionReady = undefined
		})
		return this.controlSessionReady
	}

	getControlSession(): WebSocket | undefined {
		return this.controlSessionSocket
	}

	reportControlSessionClosed(reason?: string | null): void {
		this.controlSessionState = 'disconnected'
		this.lastControlDisconnectedAt = Date.now()
		this.lastControlCloseReason = reason || 'closed'
		this.failAllPendingControlCommands(
			new Error(reason || 'events websocket closed'),
			'closed',
		)
		this.publishSnapshot()
	}

	reportSyncPathState(
		path: 'webrtc_data:yjs' | 'yws' | null,
		state: 'idle' | 'connecting' | 'connected' | 'disconnected',
	): void {
		this.syncPathEvidence = { path, state }
		this.publishSnapshot()
	}

	private resolveDirectPathPolicy(): {
		enabled: boolean
		source: HubMemberDirectPolicySource
	} {
		try {
			const url = new URL(window.location.href)
			const p2p = (url.searchParams.get('p2p') || '').trim().toLowerCase()
			if (p2p === '1' || p2p === 'true') {
				return { enabled: true, source: 'query' }
			}
			if (p2p === '0' || p2p === 'false') {
				return { enabled: false, source: 'query' }
			}
			const webrtc = (url.searchParams.get('webrtc') || '').trim().toLowerCase()
			if (webrtc === '1' || webrtc === 'true') {
				return { enabled: true, source: 'query' }
			}
			if (webrtc === '0' || webrtc === 'false') {
				return { enabled: false, source: 'query' }
			}
		} catch {}

		try {
			const persisted = (localStorage.getItem('adaos_p2p') || '').trim()
			if (persisted === '1') {
				return { enabled: true, source: 'storage' }
			}
			if (persisted === '0') {
				return { enabled: false, source: 'storage' }
			}
		} catch {}

		return { enabled: true, source: 'default' }
	}

	async prepareDirectPaths(
		sendCommand: (
			kind: string,
			payload: Record<string, any>,
		) => Promise<any>,
		{
			remoteProxy = false,
		}: {
			remoteProxy?: boolean
		} = {},
	): Promise<boolean> {
		const policy = this.resolveDirectPathPolicy()
		this.directRemoteProxyEligible = remoteProxy
		this.directSignalCommand = sendCommand
		this.lastDirectNegotiationAt = Date.now()
		if (!remoteProxy || !policy.enabled) {
			this.lastDirectNegotiationState = 'skipped'
			this.publishSnapshot()
			return false
		}
		const ok = await this.rtc.negotiate(sendCommand)
		this.lastDirectNegotiationState = ok ? 'connected' : 'failed'
		this.noteDirectRecoveryResult(ok, Date.now(), 'full_renegotiate')
		this.publishSnapshot()
		return ok
	}

	initRuntime(): void {
		if (!this.runtimeInitialized) {
			this.runtimeInitialized = true
			this.installVisibilityTracking()
		}
		this.publishSnapshot()
	}

	private installVisibilityTracking(): void {
		if (this.visibilityTrackingInstalled || typeof document === 'undefined') {
			return
		}
		this.visibilityTrackingInstalled = true
		this.isPageVisible = !document.hidden
		document.addEventListener('visibilitychange', () => {
			const nowVisible = !document.hidden
			if (this.isPageVisible === nowVisible) {
				return
			}
			this.isPageVisible = nowVisible
			if (!nowVisible) {
				return
			}
			this.scheduleDirectRecovery()
		})
	}

	private scheduleDirectRecovery(): void {
		if (this.directRecoveryDebounce) {
			clearTimeout(this.directRecoveryDebounce)
		}
		const nowMs = Date.now()
		const waitMs = this.nextDirectRecoveryAt
			? Math.max(
					HubMemberChannelsService.DIRECT_RECOVERY_DEBOUNCE_MS,
					this.nextDirectRecoveryAt - nowMs,
			  )
			: HubMemberChannelsService.DIRECT_RECOVERY_DEBOUNCE_MS
		this.directRecoveryDebounce = setTimeout(() => {
			void this.attemptDirectRecovery()
		}, waitMs)
	}

	resolveActivePath(
		channelId: HubMemberSemanticChannelId,
		nowMs = Date.now(),
	): HubMemberSemanticPath | null {
		return this.resolveState(channelId, nowMs).activePath
	}

	sendEventsEnvelope(
		ws: WebSocket,
		json: string,
		{
			channelId = 'command',
			forceWs = false,
		}: {
			channelId?: 'command' | 'event' | 'presence'
			forceWs?: boolean
		} = {},
	): HubMemberSemanticPath {
		if (!forceWs) {
			const path = this.resolveActivePath(channelId)
			if (path === 'webrtc_data:events' && this.rtc.sendEvents(json)) {
				this.publishSnapshot()
				return path
			}
		}
		ws.send(json)
		this.forceActivePath(channelId, 'ws')
		return 'ws'
	}

	resolveCommandPolicy(kind: string): HubMemberCommandPolicy {
		const normalized = String(kind || '').trim().toLowerCase()
		if (normalized.startsWith('rtc.')) {
			return 'control_ws'
		}
		return 'member_command'
	}

	sendCommandEnvelope(
		ws: WebSocket,
		kind: string,
		json: string,
	): HubMemberSemanticPath {
		if (this.resolveCommandPolicy(kind) === 'control_ws') {
			return this.sendControlEnvelope(ws, json)
		}
		return this.sendEventsEnvelope(ws, json, {
			channelId: 'command',
		})
	}

	createPendingControlCommand(
		kind: string,
		payload: Record<string, any>,
		timeoutMs: number,
	): {
		id: string
		json: string
		ack: Promise<any>
	} {
		const id = `${kind}.${Date.now()}.${Math.random()
			.toString(16)
			.slice(2)}`
		const envelope = {
			ch: 'events' as const,
			t: 'cmd' as const,
			id,
			kind,
			payload: payload ?? {},
		}
		const ack = new Promise<any>((resolve, reject) => {
			const timeout = setTimeout(() => {
				this.failPendingControlCommand(
					id,
					new Error(`events command timeout: ${kind}`),
					'timeout',
				)
			}, timeoutMs)
			this.pendingControlCommands.set(id, {
				kind,
				resolve,
				reject,
				timeout,
			})
		})
		this.publishSnapshot()
		return {
			id,
			json: JSON.stringify(envelope),
			ack,
		}
	}

	handleIncomingControlMessage(raw: string): { handled: boolean; ack?: any } {
		try {
			const msg = JSON.parse(raw)
			if (msg?.ch === 'events' && msg?.t === 'ack' && msg?.id) {
				const id = String(msg.id)
				const pending = this.pendingControlCommands.get(id)
				if (pending) {
					clearTimeout(pending.timeout)
					this.pendingControlCommands.delete(id)
					this.lastControlCommandKind = pending.kind
					this.lastControlCommandCompletedAt = Date.now()
					this.lastControlCommandOutcome = 'ack'
					pending.resolve(msg)
					this.publishSnapshot()
				}
				return { handled: true, ack: msg }
			}
			if (msg?.ch === 'events' && msg?.kind === 'rtc.ice') {
				this.rtc.acceptRemoteIceCandidate(msg?.payload?.candidate)
				return { handled: true }
			}
			if (
				msg?.ch === 'events' &&
				msg?.kind === 'rtc.answer' &&
				msg?.payload?.sdp
			) {
				this.rtc.acceptRemoteAnswer({
					type: msg?.payload?.type || 'answer',
					sdp: msg?.payload?.sdp,
				})
				return { handled: true }
			}
		} catch {}
		return { handled: false }
	}

	sendControlEnvelope(ws: WebSocket, json: string): 'ws' {
		ws.send(json)
		return 'ws'
	}

	registerControlSubscriptions(topics: string[]): string[] {
		const added: string[] = []
		for (const raw of topics) {
			const topic = String(raw || '').trim()
			if (!topic || this.controlSubscriptions.has(topic)) {
				continue
			}
			this.controlSubscriptions.add(topic)
			added.push(topic)
		}
		if (added.length) {
			this.publishSnapshot()
		}
		return added
	}

	onControlWsOpen(ws: WebSocket): void {
		this.controlSessionState = 'connected'
		this.controlSessionOpenCount += 1
		this.lastControlConnectedAt = Date.now()
		this.lastControlCloseReason = null
		const topics = [...this.controlSubscriptions]
		if (!topics.length) {
			this.publishSnapshot()
			return
		}
		this.sendControlSubscriptions(ws, topics)
	}

	failPendingControlCommand(
		id: string,
		error: any,
		outcome: Exclude<HubMemberControlCommandOutcome, 'ack'>,
	): void {
		const pending = this.pendingControlCommands.get(id)
		if (!pending) {
			return
		}
		clearTimeout(pending.timeout)
		this.pendingControlCommands.delete(id)
		this.lastControlCommandKind = pending.kind
		this.lastControlCommandCompletedAt = Date.now()
		this.lastControlCommandOutcome = outcome
		pending.reject(error)
		this.publishSnapshot()
	}

	failAllPendingControlCommands(
		error: any,
		outcome: Exclude<HubMemberControlCommandOutcome, 'ack'> = 'closed',
	): void {
		for (const id of [...this.pendingControlCommands.keys()]) {
			this.failPendingControlCommand(id, error, outcome)
		}
	}

	sendControlSubscriptions(ws: WebSocket, topics: string[]): number {
		const normalized = topics
			.map((raw) => String(raw || '').trim())
			.filter((topic, index, arr) => !!topic && arr.indexOf(topic) === index)
		if (!normalized.length) {
			return 0
		}
		this.sendControlEnvelope(
			ws,
			JSON.stringify({ type: 'subscribe', topics: normalized }),
		)
		this.lastControlReplayAt = Date.now()
		this.lastControlReplayCount = normalized.length
		this.publishSnapshot()
		return normalized.length
	}

	private async sendRtcSignal(
		kind: string,
		payload: Record<string, any>,
	): Promise<void> {
		if (!this.directSignalCommand) {
			return
		}
		try {
			await this.directSignalCommand(kind, payload)
		} catch {
			// Direct-path probing is best-effort; semantic recovery policy handles retries.
		}
	}

	private isDirectRecoveryEligible(nowMs = Date.now()): boolean {
		const policy = this.resolveDirectPathPolicy()
		const rtc = this.rtc.getRecoverySnapshot()
		const lastConnectedAt = rtc.lastConnectedAt ?? 0
		const connectionStale =
			!lastConnectedAt ||
			nowMs - lastConnectedAt >
				HubMemberChannelsService.DIRECT_RECOVERY_HEALTH_CHECK_MS
		if (!this.directRemoteProxyEligible || !policy.enabled) {
			return false
		}
		if (this.nextDirectRecoveryAt && nowMs < this.nextDirectRecoveryAt) {
			return false
		}
		if (this.wsState !== 'connected') {
			return false
		}
		return this.resolveDirectRecoveryMode(nowMs, rtc) !== null || (
			rtc.state !== 'connected' && connectionStale && rtc.canRenegotiate
		)
	}

	private async attemptDirectRecovery(): Promise<boolean> {
		if (this.directRecoveryInFlight) {
			return this.directRecoveryInFlight
		}
		const run = (async () => {
			const nowMs = Date.now()
			this.lastDirectRecoveryAt = nowMs
			const rtc = this.rtc.getRecoverySnapshot()
			const mode = this.resolveDirectRecoveryMode(nowMs, rtc)
			if (!mode) {
				this.lastDirectRecoveryMode = null
				this.lastDirectRecoveryState = 'skipped'
				this.publishSnapshot()
				return false
			}
			this.lastDirectRecoveryMode = mode
			this.directRecoveryAttemptCount += 1
			const ok =
				mode === 'ice_restart'
					? await this.rtc.restartIceTransport()
					: await this.rtc.triggerFullRenegotiation()
			this.lastDirectRecoveryState = ok ? 'recovered' : 'failed'
			this.noteDirectRecoveryResult(ok, Date.now(), mode)
			this.publishSnapshot()
			return ok
		})()
		this.directRecoveryInFlight = run.finally(() => {
			this.directRecoveryInFlight = undefined
		})
		return this.directRecoveryInFlight
	}

	private resolveDirectRecoveryMode(
		nowMs: number,
		rtc = this.rtc.getRecoverySnapshot(),
	): HubMemberDirectRecoveryMode | null {
		const policy = this.resolveDirectPathPolicy()
		if (!this.directRemoteProxyEligible || !policy.enabled) {
			return null
		}
		if (this.wsState !== 'connected') {
			return null
		}
		if (this.nextDirectRecoveryAt && nowMs < this.nextDirectRecoveryAt) {
			return null
		}
		if (
			rtc.state === 'disconnected' &&
			rtc.lastDisconnectedAt &&
			nowMs - rtc.lastDisconnectedAt <
				HubMemberChannelsService.DIRECT_RECOVERY_DISCONNECT_GRACE_MS
		) {
			return null
		}
		if (
			rtc.state === 'disconnected' &&
			rtc.canRestartIce &&
			this.directRecoveryIceRestartCount <
				HubMemberChannelsService.DIRECT_RECOVERY_ICE_RESTART_LIMIT
		) {
			return 'ice_restart'
		}
		if (
			(rtc.state === 'failed' || rtc.state === 'disconnected') &&
			rtc.canRenegotiate
		) {
			return 'full_renegotiate'
		}
		const lastConnectedAt = rtc.lastConnectedAt ?? 0
		const connectionStale =
			!lastConnectedAt ||
			nowMs - lastConnectedAt >
				HubMemberChannelsService.DIRECT_RECOVERY_HEALTH_CHECK_MS
		if (rtc.state !== 'connected' && connectionStale && rtc.canRenegotiate) {
			return 'full_renegotiate'
		}
		return null
	}

	createSyncProvider(
		doc: Y.Doc,
		serverUrl: string,
		room: string,
		params: Record<string, string>,
		{
			recoveryReason = null,
		}: {
			recoveryReason?: HubMemberSyncRecoveryReason | null
		} = {},
	): {
		provider: WebsocketProvider | DataChannelProvider
		path: HubMemberSemanticPath
	} {
		const nowMs = Date.now()
		const path = this.resolveActivePath('sync')
		this.lastSyncProviderCreateAt = nowMs
		if (path === 'webrtc_data:yjs') {
			const dc = this.rtc.getYjsChannel()
			if (dc && dc.readyState === 'open') {
				this.lastSyncProviderPath = 'webrtc_data:yjs'
				if (recoveryReason) {
					this.lastSyncRecoveryAt = nowMs
					this.lastSyncRecoveryReason = recoveryReason
					this.lastSyncRecoveryState = 'recreated'
					this.syncRecoveryCount += 1
				}
				this.reportSyncPathState('webrtc_data:yjs', 'connecting')
				this.publishSnapshot()
				return {
					provider: new DataChannelProvider(doc, dc),
					path: 'webrtc_data:yjs',
				}
			}
		}
		this.forceActivePath('sync', 'yws')
		this.lastSyncProviderPath = 'yws'
		if (recoveryReason) {
			this.lastSyncRecoveryAt = nowMs
			this.lastSyncRecoveryReason = recoveryReason
			this.lastSyncRecoveryState = 'recreated'
			this.syncRecoveryCount += 1
		}
		this.reportSyncPathState('yws', 'connecting')
		return {
			provider: new WebsocketProvider(serverUrl, room, doc, {
				params,
			}),
			path: 'yws',
		}
	}

	shouldRecoverSyncProvider(
		{
			path,
			remoteProxy,
			hasSeededContent,
		}: {
			path: HubMemberSemanticPath | null
			remoteProxy: boolean
			hasSeededContent: boolean
		},
		nowMs = Date.now(),
	): boolean {
		if (!remoteProxy || path !== 'yws' || hasSeededContent) {
			return false
		}
		if (this.wsState !== 'connected') {
			return false
		}
		if (
			this.lastSyncRecoveryAt &&
			nowMs - this.lastSyncRecoveryAt <
				HubMemberChannelsService.SYNC_RECOVERY_DEBOUNCE_MS
		) {
			return false
		}
		return true
	}

	recordSyncRecoverySkipped(reason: HubMemberSyncRecoveryReason): void {
		this.lastSyncRecoveryAt = Date.now()
		this.lastSyncRecoveryReason = reason
		this.lastSyncRecoveryState = 'skipped'
		this.publishSnapshot()
	}

	recordSyncRecoveryFailed(reason: HubMemberSyncRecoveryReason): void {
		this.lastSyncRecoveryAt = Date.now()
		this.lastSyncRecoveryReason = reason
		this.lastSyncRecoveryState = 'failed'
		this.publishSnapshot()
	}

	getSnapshot(): HubMemberChannelSnapshot {
		return this.buildSnapshot()
	}

	private onControlSessionMessage = (ev: MessageEvent): void => {
		const data = typeof ev.data === 'string' ? ev.data : ''
		this.handleIncomingControlMessage(data)
	}

	private onControlSessionClose = (ev: CloseEvent): void => {
		this.resetControlSession(ev)
	}

	private resetControlSession(reason?: any): void {
		if (this.controlSessionSocket) {
			try {
				this.controlSessionSocket.removeEventListener(
					'message',
					this.onControlSessionMessage,
				)
				this.controlSessionSocket.removeEventListener(
					'close',
					this.onControlSessionClose,
				)
			} catch {}
		}
		this.controlSessionSocket = undefined
		this.controlSessionReady = undefined
		this.reportWsState('disconnected')
		this.reportControlSessionClosed(
			this.describeControlSessionReason(reason),
		)
	}

	private describeControlSessionReason(reason: any): string {
		if (typeof reason === 'string' && reason.trim()) return reason.trim()
		if (reason instanceof Error && reason.message) return reason.message
		if (typeof reason?.reason === 'string' && reason.reason.trim()) {
			return reason.reason.trim()
		}
		if (typeof reason?.type === 'string' && reason.type.trim()) {
			return reason.type.trim()
		}
		return 'closed'
	}

	private resolveState(
		channelId: HubMemberSemanticChannelId,
		nowMs: number,
	): {
		activePath: HubMemberSemanticPath | null
		preferredPath: HubMemberSemanticPath | null
		availablePaths: HubMemberSemanticPath[]
		freezeRemainingMs: number
	} {
		const spec = CHANNEL_SPECS[channelId]
		const state = this.ensureState(channelId)
		const availablePaths = spec.paths.filter((path) =>
			this.isPathAvailable(path),
		)
		const connectedPaths = availablePaths.filter(
			(path) => this.getPathEvidenceState(path) === 'connected',
		)
		const preferredPath = availablePaths[0] || null
		const preferredConnectedPath = connectedPaths[0] || null
		const currentPath =
			state.activePath && availablePaths.includes(state.activePath)
				? state.activePath
				: null
		const currentConnectedPath =
			currentPath && this.getPathEvidenceState(currentPath) === 'connected'
				? currentPath
				: null
		let activePath = preferredPath
		let freezeRemainingMs = 0

		if (currentConnectedPath) {
			activePath = currentConnectedPath
			if (
				preferredConnectedPath &&
				currentConnectedPath !== preferredConnectedPath &&
				spec.freezeMs > 0
			) {
				const elapsed = Math.max(0, nowMs - state.lastSwitchAt)
				if (elapsed < spec.freezeMs) {
					activePath = currentConnectedPath
					freezeRemainingMs = spec.freezeMs - elapsed
				} else {
					activePath = preferredConnectedPath
				}
			} else if (preferredConnectedPath) {
				activePath = preferredConnectedPath
			}
		} else if (preferredConnectedPath) {
			activePath = preferredConnectedPath
		} else if (currentPath) {
			activePath = currentPath
		}

		if (state.activePath !== activePath) {
			state.previousPath = state.activePath
			state.activePath = activePath
			state.preferredPath = preferredPath
			state.lastSwitchAt = nowMs
			state.switchTotal += 1
		} else {
			state.preferredPath = preferredPath
		}

		return {
			activePath,
			preferredPath,
			availablePaths: [...availablePaths],
			freezeRemainingMs,
		}
	}

	private forceActivePath(
		channelId: HubMemberSemanticChannelId,
		path: HubMemberSemanticPath,
	): void {
		const state = this.ensureState(channelId)
		if (state.activePath === path) {
			this.publishSnapshot()
			return
		}
		state.previousPath = state.activePath
		state.activePath = path
		state.preferredPath = path
		state.lastSwitchAt = Date.now()
		state.switchTotal += 1
		this.publishSnapshot()
	}

	private ensureState(channelId: HubMemberSemanticChannelId): ChannelState {
		const existing = this.states.get(channelId)
		if (existing) return existing
		const state: ChannelState = {
			activePath: null,
			preferredPath: null,
			previousPath: null,
			lastSwitchAt: 0,
			switchTotal: 0,
		}
		this.states.set(channelId, state)
		return state
	}

	private isPathAvailable(path: HubMemberSemanticPath): boolean {
		const state = this.getPathEvidenceState(path)
		return state === 'connecting' || state === 'connected'
	}

	private buildSnapshot(nowMs = Date.now()): HubMemberChannelSnapshot {
		const rtc = this.rtc.getRecoverySnapshot()
		const pathEvidence = this.buildPathEvidence()
		const channels = {} as HubMemberChannelSnapshot['channels']
		for (const channelId of Object.keys(
			CHANNEL_SPECS,
		) as HubMemberSemanticChannelId[]) {
			const state = this.resolveState(channelId, nowMs)
			channels[channelId] = {
				activePath: state.activePath,
				preferredPath: state.preferredPath,
				availablePaths: state.availablePaths,
				freezeRemainingMs: state.freezeRemainingMs,
				switchTotal: this.ensureState(channelId).switchTotal,
				activeReady: !!(
					state.activePath &&
					pathEvidence[state.activePath]?.state === 'connected'
				),
				health: this.computeChannelHealth(
					channelId,
					state.activePath,
					state.preferredPath,
					state.availablePaths,
					pathEvidence,
				),
			}
		}
		return {
			updatedAt: nowMs,
			directPolicy: {
				...this.resolveDirectPathPolicy(),
				lastNegotiationAt: this.lastDirectNegotiationAt,
				lastNegotiationState: this.lastDirectNegotiationState,
			},
			directRecovery: {
				eligible: this.isDirectRecoveryEligible(nowMs),
				lastAttemptAt: this.lastDirectRecoveryAt,
				lastAttemptState: this.lastDirectRecoveryState,
				lastAttemptMode: this.lastDirectRecoveryMode,
				attemptCount: this.directRecoveryAttemptCount,
				failureCount: this.directRecoveryFailureCount,
				nextAttemptAt: this.nextDirectRecoveryAt,
			},
			syncRecovery: {
				eligible: this.shouldRecoverSyncProvider(
					{
						path: this.currentSyncPath(),
						remoteProxy: this.directRemoteProxyEligible,
						hasSeededContent: false,
					},
					nowMs,
				),
				lastAttemptAt: this.lastSyncRecoveryAt,
				lastAttemptState: this.lastSyncRecoveryState,
				lastReason: this.lastSyncRecoveryReason,
				recoveryCount: this.syncRecoveryCount,
				lastCreatedPath: this.lastSyncProviderPath,
				lastCreatedAt: this.lastSyncProviderCreateAt,
			},
			controlPlane: {
				sessionState: this.controlSessionState,
				sessionOpenCount: this.controlSessionOpenCount,
				lastConnectedAt: this.lastControlConnectedAt,
				lastDisconnectedAt: this.lastControlDisconnectedAt,
				lastCloseReason: this.lastControlCloseReason,
				subscriptionsTracked: this.controlSubscriptions.size,
				pendingCommands: this.pendingControlCommands.size,
				lastCommandKind: this.lastControlCommandKind,
				lastCommandCompletedAt: this.lastControlCommandCompletedAt,
				lastCommandOutcome: this.lastControlCommandOutcome,
				lastReplayAt: this.lastControlReplayAt,
				lastReplayCount: this.lastControlReplayCount,
			},
			rtcRuntime: {
				state: rtc.state,
				iceConnectionState: rtc.iceConnectionState,
				connectionState: rtc.connectionState,
				lastConnectedAt: rtc.lastConnectedAt,
				lastDisconnectedAt: rtc.lastDisconnectedAt,
				lastFailureAt: rtc.lastFailureAt,
				lastFailureReason: rtc.lastFailureReason,
				canRestartIce: rtc.canRestartIce,
				canRenegotiate: rtc.canRenegotiate,
			},
			mediaPolicy: {
				mode: 'out_of_scope',
				reason: 'phase4_media_semantics_not_implemented',
			},
			pathEvidence,
			channels,
		}
	}

	private buildTransportState(
		snapshot: HubMemberChannelSnapshot,
	): HubMemberTransportState {
		const rtcState = this.rtc.state$.value
		const commandPath = snapshot.channels.command.activePath
		const syncPath = snapshot.channels.sync.activePath
		const commandReady = !!(
			commandPath &&
			snapshot.pathEvidence[commandPath]?.state === 'connected'
		)
		const syncReady = !!(
			syncPath &&
			snapshot.pathEvidence[syncPath]?.state === 'connected'
		)
		if (
			(commandPath === 'webrtc_data:events' && commandReady) ||
			(syncPath === 'webrtc_data:yjs' && syncReady)
		) {
			return 'connected'
		}
		if (
			(commandPath === 'ws' && commandReady) ||
			(syncPath === 'yws' && syncReady) ||
			snapshot.controlPlane.sessionState === 'connected'
		) {
			return 'ws'
		}
		if (rtcState === 'signaling' || rtcState === 'connecting') {
			return rtcState
		}
		if (
			snapshot.pathEvidence.ws.state === 'connecting' ||
			snapshot.pathEvidence.yws.state === 'connecting'
		) {
			return 'connecting'
		}
		return 'ws'
	}

	private computeChannelHealth(
		channelId: HubMemberSemanticChannelId,
		activePath: HubMemberSemanticPath | null,
		preferredPath: HubMemberSemanticPath | null,
		availablePaths: HubMemberSemanticPath[],
		pathEvidence: HubMemberChannelSnapshot['pathEvidence'],
	): HubMemberChannelHealth {
		if (channelId === 'route') {
			if (
				this.controlSessionState === 'connected' &&
				this.wsState === 'connected'
			) {
				return 'ready'
			}
			if (
				this.controlSessionState === 'connecting' ||
				this.wsState === 'connecting'
			) {
				return 'recovering'
			}
			if (this.wsState === 'connected') {
				return 'fallback'
			}
			return 'degraded'
		}
		const spec = CHANNEL_SPECS[channelId]
		if (!spec.paths.length) {
			return 'unavailable'
		}
		const activeState = activePath ? pathEvidence[activePath]?.state : null
		const preferredState = preferredPath
			? pathEvidence[preferredPath]?.state
			: null
		if (activePath && activeState === 'connected') {
			if (
				activePath === preferredPath &&
				preferredState === 'connected'
			) {
				return 'ready'
			}
			return 'fallback'
		}
		const hasConnectedFallback = availablePaths.some(
			(path) => pathEvidence[path]?.state === 'connected',
		)
		if (
			(activePath && activeState === 'connecting') ||
			(preferredPath && preferredState === 'connecting')
		) {
			if (hasConnectedFallback) {
				return 'fallback'
			}
			return 'recovering'
		}
		if (availablePaths.length > 0) {
			return 'degraded'
		}
		return 'degraded'
	}

	private noteDirectRecoveryResult(
		ok: boolean,
		nowMs: number,
		mode?: HubMemberDirectRecoveryMode | null,
	): void {
		if (ok) {
			if (mode === 'full_renegotiate') {
				this.directRecoveryIceRestartCount = 0
			}
			this.directRecoveryFailureCount = 0
			this.nextDirectRecoveryAt = null
			return
		}
		if (mode === 'ice_restart') {
			this.directRecoveryIceRestartCount += 1
		} else if (mode === 'full_renegotiate') {
			this.directRecoveryIceRestartCount = 0
		}
		this.directRecoveryFailureCount += 1
		const backoffMs = Math.min(
			HubMemberChannelsService.DIRECT_RECOVERY_BACKOFF_BASE_MS *
				Math.pow(2, Math.max(0, this.directRecoveryFailureCount - 1)),
			HubMemberChannelsService.DIRECT_RECOVERY_BACKOFF_MAX_MS,
		)
		this.nextDirectRecoveryAt = nowMs + backoffMs
	}

	private buildPathEvidence(): HubMemberChannelSnapshot['pathEvidence'] {
		return {
			ws: {
				state: this.wsState,
			},
			yws: {
				state:
					this.syncPathEvidence.path === 'yws'
						? this.syncPathEvidence.state
						: 'idle',
			},
			'webrtc_data:events': {
				state: this.getRtcPathState('events'),
			},
			'webrtc_data:yjs': {
				state:
					this.syncPathEvidence.path === 'webrtc_data:yjs'
						? this.syncPathEvidence.state
						: this.getRtcPathState('yjs'),
			},
		}
	}

	private currentSyncPath(): HubMemberSemanticPath | null {
		if (this.syncPathEvidence.path === 'webrtc_data:yjs') {
			return 'webrtc_data:yjs'
		}
		if (this.syncPathEvidence.path === 'yws') {
			return 'yws'
		}
		const activeSyncPath = this.ensureState('sync').activePath
		return activeSyncPath === 'webrtc_data:yjs' || activeSyncPath === 'yws'
			? activeSyncPath
			: null
	}

	private getPathEvidenceState(
		path: HubMemberSemanticPath,
	): HubMemberPathEvidenceState {
		if (path === 'ws') return this.wsState
		if (path === 'yws') {
			return this.syncPathEvidence.path === 'yws'
				? this.syncPathEvidence.state
				: 'idle'
		}
		if (path === 'webrtc_data:events') {
			return this.getRtcPathState('events')
		}
		if (path === 'webrtc_data:yjs') {
			return this.syncPathEvidence.path === 'webrtc_data:yjs'
				? this.syncPathEvidence.state
				: this.getRtcPathState('yjs')
		}
		return 'disconnected'
	}

	private getRtcPathState(
		channel: 'events' | 'yjs',
	): HubMemberPathEvidenceState {
		const rtcState = this.rtc.state$.value
		const dc =
			channel === 'events'
				? this.rtc.getEventsChannel()
				: this.rtc.getYjsChannel()
		if (dc?.readyState === 'open' && this.rtc.isConnected()) {
			return 'connected'
		}
		if (
			dc?.readyState === 'connecting' ||
			rtcState === 'signaling' ||
			rtcState === 'connecting'
		) {
			return 'connecting'
		}
		return 'disconnected'
	}

	private publishSnapshot(): void {
		const snapshot = this.buildSnapshot()
		this.snapshot$.next(snapshot)
		this.transportState$.next(this.buildTransportState(snapshot))
	}
}
