import { Injectable } from '@angular/core'
import { BehaviorSubject } from 'rxjs'

export type RtcTransportState =
	| 'idle'
	| 'signaling'
	| 'disconnected'
	| 'connecting'
	| 'connected'
	| 'failed'

export type RtcTransportRecoverySnapshot = {
	state: RtcTransportState
	lastConnectedAt: number | null
	lastDisconnectedAt: number | null
	lastFailureAt: number | null
	lastFailureReason: string | null
	iceConnectionState: RTCIceConnectionState | 'closed' | 'new'
	connectionState: RTCPeerConnectionState | 'closed' | 'new'
	canRestartIce: boolean
	canRenegotiate: boolean
}

/**
 * Low-level WebRTC executor for the browser member transport.
 *
 * Semantic channel policy owns control-plane routing, retry/backoff, and
 * failover authority. This service only owns peer lifecycle, SDP/ICE
 * execution, and DataChannel callbacks.
 */
@Injectable({ providedIn: 'root' })
export class WebRtcTransportService {
	private static readonly STUN_SERVERS: RTCIceServer[] = [
		{ urls: 'stun:stun.l.google.com:19302' },
		{ urls: 'stun:stun1.l.google.com:19302' },
	]
	private static readonly CONNECT_TIMEOUT_MS = 8000

	private pc: RTCPeerConnection | null = null
	private eventsChannel: RTCDataChannel | null = null
	private yjsChannel: RTCDataChannel | null = null
	private lastConnectedTimestamp = 0
	private lastDisconnectedTimestamp = 0
	private lastFailureTimestamp = 0
	private lastFailureReason: string | null = null
	private pendingSendCommand:
		| ((kind: string, payload: Record<string, any>) => Promise<any>)
		| null = null

	private answerResolve: ((sdp: RTCSessionDescriptionInit) => void) | null =
		null
	private answerReject: ((err: Error) => void) | null = null

	readonly state$ = new BehaviorSubject<RtcTransportState>('idle')

	onEventsMessage: ((data: string) => void) | null = null
	onYjsMessage: ((data: ArrayBuffer) => void) | null = null
	onLocalIceCandidate: ((candidate: RTCIceCandidateInit) => void) | null = null

	async negotiate(
		sendCommand: (
			kind: string,
			payload: Record<string, any>,
		) => Promise<any>,
	): Promise<boolean> {
		this.close()
		this.state$.next('signaling')

		try {
			return await this.doNegotiate(sendCommand)
		} catch (err) {
			this.lastFailureTimestamp = Date.now()
			this.lastFailureReason =
				err instanceof Error && err.message ? err.message : 'negotiate_failed'
			this.state$.next('failed')
			return false
		}
	}

	sendEvents(data: string): boolean {
		if (this.eventsChannel?.readyState === 'open') {
			this.eventsChannel.send(data)
			return true
		}
		return false
	}

	sendYjs(data: ArrayBuffer | Uint8Array): boolean {
		if (this.yjsChannel?.readyState === 'open') {
			this.yjsChannel.send(data)
			return true
		}
		return false
	}

	isConnected(): boolean {
		return this.state$.value === 'connected'
	}

	getYjsChannel(): RTCDataChannel | null {
		return this.yjsChannel
	}

	getEventsChannel(): RTCDataChannel | null {
		return this.eventsChannel
	}

	getRecoverySnapshot(): RtcTransportRecoverySnapshot {
		return {
			state: this.state$.value,
			lastConnectedAt: this.lastConnectedTimestamp || null,
			lastDisconnectedAt: this.lastDisconnectedTimestamp || null,
			lastFailureAt: this.lastFailureTimestamp || null,
			lastFailureReason: this.lastFailureReason,
			iceConnectionState: this.pc?.iceConnectionState || 'new',
			connectionState: this.pc?.connectionState || 'new',
			canRestartIce: !!(this.pc && this.pendingSendCommand),
			canRenegotiate: !!this.pendingSendCommand,
		}
	}

	close(): void {
		if (this.eventsChannel) {
			try {
				this.eventsChannel.close()
			} catch {}
			this.eventsChannel = null
		}
		if (this.yjsChannel) {
			try {
				this.yjsChannel.close()
			} catch {}
			this.yjsChannel = null
		}
		if (this.pc) {
			try {
				this.pc.close()
			} catch {}
			this.pc = null
		}
		try {
			this.answerReject?.(new Error('rtc_closed'))
		} catch {}
		this.answerResolve = null
		this.answerReject = null
		this.pendingSendCommand = null
		this.lastFailureReason = null
		this.state$.next('idle')
	}

	async triggerFullRenegotiation(): Promise<boolean> {
		if (!this.pendingSendCommand) {
			console.warn('Cannot renegotiate: missing sendCommand')
			return false
		}
		return await this.negotiate(this.pendingSendCommand)
	}

	async restartIceTransport(): Promise<boolean> {
		if (!this.pc || !this.pendingSendCommand) {
			console.warn('Cannot restart ICE: missing peer connection or sendCommand')
			return false
		}

		this.state$.next('connecting')

		try {
			const offer = await this.pc.createOffer({ iceRestart: true })
			await this.pc.setLocalDescription(offer)

			const ack = await this.pendingSendCommand('rtc.offer', {
				sdp: offer.sdp,
				type: offer.type,
			})

			if (ack?.data?.sdp) {
				await this.pc.setRemoteDescription({
					type: ack.data.type || 'answer',
					sdp: ack.data.sdp,
				})
			}
			this.lastFailureReason = null
			return true
		} catch (err) {
			this.lastFailureTimestamp = Date.now()
			this.lastFailureReason =
				err instanceof Error && err.message ? err.message : 'ice_restart_failed'
			this.state$.next('failed')
			return false
		}
	}

	acceptRemoteIceCandidate(candidate: RTCIceCandidateInit | null | undefined): void {
		if (!candidate || !this.pc) {
			return
		}
		this.pc.addIceCandidate(new RTCIceCandidate(candidate)).catch(() => {})
	}

	acceptRemoteAnswer(answer: RTCSessionDescriptionInit | null | undefined): void {
		if (!answer?.sdp) {
			return
		}
		this.answerResolve?.({
			type: answer.type || 'answer',
			sdp: answer.sdp,
		})
	}

	private async doNegotiate(
		sendCommand: (
			kind: string,
			payload: Record<string, any>,
		) => Promise<any>,
	): Promise<boolean> {
		const pc = new RTCPeerConnection({
			iceServers: WebRtcTransportService.STUN_SERVERS,
		})
		this.pc = pc

		this.eventsChannel = pc.createDataChannel('events', { ordered: true })
		this.yjsChannel = pc.createDataChannel('yjs', {
			ordered: true,
			protocol: 'binary',
		})
		this.yjsChannel.binaryType = 'arraybuffer'

		this.wireChannelCallbacks()
		this.wireConnectionStateHandlers(pc, sendCommand)

		pc.onicecandidate = (ev) => {
			if (ev.candidate) {
				this.onLocalIceCandidate?.({
					candidate: ev.candidate.candidate,
					sdpMid: ev.candidate.sdpMid,
					sdpMLineIndex: ev.candidate.sdpMLineIndex,
				})
			}
		}

		const offer = await pc.createOffer()
		await pc.setLocalDescription(offer)

		const answerPromise = new Promise<RTCSessionDescriptionInit>(
			(resolve, reject) => {
				this.answerResolve = resolve
				this.answerReject = reject
			},
		)

		const ack = await sendCommand('rtc.offer', {
			sdp: offer.sdp,
			type: offer.type,
		})

		if (ack?.data?.sdp) {
			await pc.setRemoteDescription({
				type: ack.data.type || 'answer',
				sdp: ack.data.sdp,
			})
		} else {
			let answerTimer: ReturnType<typeof setTimeout> | null = null
			try {
				const answer = await Promise.race([
					answerPromise,
					new Promise<never>((_resolve, reject) => {
						answerTimer = setTimeout(
							() => reject(new Error('rtc_answer_timeout')),
							WebRtcTransportService.CONNECT_TIMEOUT_MS,
						)
					}),
				])
				await pc.setRemoteDescription(answer)
			} finally {
				if (answerTimer) clearTimeout(answerTimer)
			}
		}

		this.answerResolve = null
		this.answerReject = null
		this.state$.next('connecting')

		let dcTimer: ReturnType<typeof setTimeout> | null = null
		try {
			await Promise.race([
				this.waitForChannelsOpen(),
				new Promise<never>((_resolve, reject) => {
					dcTimer = setTimeout(
						() => reject(new Error('dc_open_timeout')),
						WebRtcTransportService.CONNECT_TIMEOUT_MS,
					)
				}),
			])
		} finally {
			if (dcTimer) clearTimeout(dcTimer)
		}

		this.lastConnectedTimestamp = Date.now()
		this.lastFailureReason = null
		this.state$.next('connected')
		return true
	}

	private waitForChannelsOpen(): Promise<void> {
		return new Promise<void>((resolve) => {
			const check = () => {
				if (
					this.eventsChannel?.readyState === 'open' &&
					this.yjsChannel?.readyState === 'open'
				) {
					resolve()
				}
			}
			check()
			if (this.eventsChannel) this.eventsChannel.onopen = () => check()
			if (this.yjsChannel) this.yjsChannel.onopen = () => check()
		})
	}

	private wireChannelCallbacks(): void {
		if (this.eventsChannel) {
			this.eventsChannel.onmessage = (ev: MessageEvent) => {
				this.onEventsMessage?.(typeof ev.data === 'string' ? ev.data : '')
			}
		}
		if (this.yjsChannel) {
			this.yjsChannel.onmessage = (ev: MessageEvent) => {
				this.onYjsMessage?.(ev.data as ArrayBuffer)
			}
		}
	}

	private wireConnectionStateHandlers(
		pc: RTCPeerConnection,
		sendCommand: (
			kind: string,
			payload: Record<string, any>,
		) => Promise<any>,
	): void {
		this.pendingSendCommand = sendCommand

		pc.oniceconnectionstatechange = () => {
			const st = pc.iceConnectionState

			if (st === 'connected' || st === 'completed') {
				this.lastConnectedTimestamp = Date.now()
				this.lastFailureReason = null
				if (this.state$.value !== 'connected') {
					this.state$.next('connected')
				}
				return
			}

			if (st === 'disconnected') {
				this.lastDisconnectedTimestamp = Date.now()
				if (this.state$.value !== 'disconnected') {
					this.state$.next('disconnected')
				}
				return
			}

			if (st === 'failed') {
				this.lastFailureTimestamp = Date.now()
				this.lastFailureReason = 'ice_connection_failed'
				this.state$.next('failed')
			}
		}
	}
}
