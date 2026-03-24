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

export type HubMemberPathEvidenceState =
	| 'idle'
	| 'connecting'
	| 'connected'
	| 'disconnected'

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

export type HubMemberChannelSnapshot = {
	updatedAt: number
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
	private readonly states = new Map<HubMemberSemanticChannelId, ChannelState>()
	private runtimeInitialized = false
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
		this.rtc.state$.subscribe(() => {
			this.publishSnapshot()
		})
	}

	reportWsState(state: 'disconnected' | 'connecting' | 'connected'): void {
		this.wsState = state
		this.publishSnapshot()
	}

	reportSyncPathState(
		path: 'webrtc_data:yjs' | 'yws' | null,
		state: 'idle' | 'connecting' | 'connected' | 'disconnected',
	): void {
		this.syncPathEvidence = { path, state }
		this.publishSnapshot()
	}

	async negotiateDirectPaths(
		signalingWs: WebSocket,
		sendCommand: (
			kind: string,
			payload: Record<string, any>,
		) => Promise<any>,
		{
			onEventsMessage,
		}: {
			onEventsMessage?: ((data: string) => void) | null
		} = {},
	): Promise<boolean> {
		this.rtc.onEventsMessage = onEventsMessage ?? null
		const ok = await this.rtc.negotiate(signalingWs, sendCommand)
		this.publishSnapshot()
		return ok
	}

	initRuntime(): void {
		if (!this.runtimeInitialized) {
			this.runtimeInitialized = true
			this.rtc.initVisibilityTracking()
		}
		this.publishSnapshot()
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

	sendControlEnvelope(ws: WebSocket, json: string): 'ws' {
		ws.send(json)
		return 'ws'
	}

	createSyncProvider(
		doc: Y.Doc,
		serverUrl: string,
		room: string,
		params: Record<string, string>,
	): {
		provider: WebsocketProvider | DataChannelProvider
		path: HubMemberSemanticPath
	} {
		const path = this.resolveActivePath('sync')
		if (path === 'webrtc_data:yjs') {
			const dc = this.rtc.getYjsChannel()
			if (dc && dc.readyState === 'open') {
				this.reportSyncPathState('webrtc_data:yjs', 'connecting')
				this.publishSnapshot()
				return {
					provider: new DataChannelProvider(doc, dc),
					path: 'webrtc_data:yjs',
				}
			}
		}
		this.forceActivePath('sync', 'yws')
		this.reportSyncPathState('yws', 'connecting')
		return {
			provider: new WebsocketProvider(serverUrl, room, doc, {
				params,
			}),
			path: 'yws',
		}
	}

	getSnapshot(): HubMemberChannelSnapshot {
		return this.buildSnapshot()
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
		const preferredPath = availablePaths[0] || null
		const currentPath =
			state.activePath && availablePaths.includes(state.activePath)
				? state.activePath
				: null
		let activePath = preferredPath
		let freezeRemainingMs = 0

		if (
			currentPath &&
			preferredPath &&
			currentPath !== preferredPath &&
			spec.freezeMs > 0
		) {
			const elapsed = Math.max(0, nowMs - state.lastSwitchAt)
			if (elapsed < spec.freezeMs) {
				activePath = currentPath
				freezeRemainingMs = spec.freezeMs - elapsed
			}
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
			}
		}
		return {
			updatedAt: nowMs,
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
