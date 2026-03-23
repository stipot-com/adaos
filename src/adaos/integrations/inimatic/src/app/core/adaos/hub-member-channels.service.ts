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

export type HubMemberSemanticPath =
	| 'webrtc_data:events'
	| 'ws'
	| 'webrtc_data:yjs'
	| 'yws'

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
	channels: Record<
		HubMemberSemanticChannelId,
		{
			activePath: HubMemberSemanticPath | null
			preferredPath: HubMemberSemanticPath | null
			availablePaths: HubMemberSemanticPath[]
			freezeRemainingMs: number
			switchTotal: number
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
}

@Injectable({ providedIn: 'root' })
export class HubMemberChannelsService {
	private readonly states = new Map<HubMemberSemanticChannelId, ChannelState>()
	readonly snapshot$ = new BehaviorSubject<HubMemberChannelSnapshot>(
		this.buildSnapshot(),
	)

	constructor(private rtc: WebRtcTransportService) {
		this.rtc.state$.subscribe(() => {
			this.publishSnapshot()
		})
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
				this.publishSnapshot()
				return {
					provider: new DataChannelProvider(doc, dc),
					path: 'webrtc_data:yjs',
				}
			}
		}
		this.forceActivePath('sync', 'yws')
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
		if (path === 'ws' || path === 'yws') return true
		if (path === 'webrtc_data:events') {
			const dc = this.rtc.getEventsChannel()
			return this.rtc.isConnected() && dc?.readyState === 'open'
		}
		if (path === 'webrtc_data:yjs') {
			const dc = this.rtc.getYjsChannel()
			return this.rtc.isConnected() && dc?.readyState === 'open'
		}
		return false
	}

	private buildSnapshot(nowMs = Date.now()): HubMemberChannelSnapshot {
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
			}
		}
		return {
			updatedAt: nowMs,
			channels,
		}
	}

	private publishSnapshot(): void {
		this.snapshot$.next(this.buildSnapshot())
	}
}
