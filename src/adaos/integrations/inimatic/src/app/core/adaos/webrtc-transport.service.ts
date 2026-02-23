import { Injectable } from '@angular/core'
import { BehaviorSubject } from 'rxjs'

export type RtcTransportState =
	| 'idle'
	| 'signaling'
	| 'connecting'
	| 'connected'
	| 'failed'

/**
 * Manages a WebRTC peer connection with two DataChannels:
 *  - **events** (ordered, reliable) — JSON commands mirroring /ws
 *  - **yjs** (ordered, reliable) — binary Yjs CRDT sync mirroring /yws
 *
 * Signaling (SDP offer/answer + ICE candidates) is performed over the
 * existing Events WebSocket which is already tunnelled via NATS.
 */
@Injectable({ providedIn: 'root' })
export class WebRtcTransportService {
	private static readonly STUN_SERVERS: RTCIceServer[] = [
		{ urls: 'stun:stun.l.google.com:19302' },
		{ urls: 'stun:stun1.l.google.com:19302' },
	]
	private static readonly CONNECT_TIMEOUT_MS = 8000
	private static readonly ICE_RESTART_MAX = 2
	private static readonly DISCONNECT_GRACE_MS = 3000

	private pc: RTCPeerConnection | null = null
	private eventsChannel: RTCDataChannel | null = null
	private yjsChannel: RTCDataChannel | null = null
	private iceRestartCount = 0
	private signalingWs: WebSocket | null = null
	private signalingListener: ((ev: MessageEvent) => void) | null = null

	/** Pending answer resolve/reject for the current negotiation round. */
	private answerResolve: ((sdp: RTCSessionDescriptionInit) => void) | null =
		null
	private answerReject: ((err: Error) => void) | null = null

	/** Observable transport state. */
	readonly state$ = new BehaviorSubject<RtcTransportState>('idle')

	/** Callback for incoming messages on the *events* DataChannel. */
	onEventsMessage: ((data: string) => void) | null = null

	/** Callback for incoming messages on the *yjs* DataChannel. */
	onYjsMessage: ((data: ArrayBuffer) => void) | null = null

	// -- public API -----------------------------------------------------------

	/**
	 * Attempt to establish a WebRTC connection to the hub.
	 *
	 * @param signalingWs  The existing Events WS (already connected through NATS tunnel).
	 * @param sendCommand  Function to send a signaling command over the WS.
	 * @returns `true` if the WebRTC DataChannels are open, `false` on failure.
	 */
	async negotiate(
		signalingWs: WebSocket,
		sendCommand: (
			kind: string,
			payload: Record<string, any>,
		) => Promise<any>,
	): Promise<boolean> {
		this.close()
		this.signalingWs = signalingWs
		this.state$.next('signaling')
		this.iceRestartCount = 0

		try {
			this.installSignalingListener(signalingWs)
			return await this.doNegotiate(sendCommand)
		} catch (err) {
			this.state$.next('failed')
			return false
		}
	}

	/** Send a JSON string on the *events* DataChannel. Returns `true` if sent. */
	sendEvents(data: string): boolean {
		if (this.eventsChannel?.readyState === 'open') {
			this.eventsChannel.send(data)
			return true
		}
		return false
	}

	/** Send binary data on the *yjs* DataChannel. Returns `true` if sent. */
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

	close(): void {
		this.removeSignalingListener()
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
		this.answerResolve = null
		this.answerReject = null
		this.state$.next('idle')
	}

	// -- internals ------------------------------------------------------------

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

		// Create DataChannels (browser is the offerer → creates channels)
		this.eventsChannel = pc.createDataChannel('events', { ordered: true })
		this.yjsChannel = pc.createDataChannel('yjs', {
			ordered: true,
			protocol: 'binary',
		})
		this.yjsChannel.binaryType = 'arraybuffer'

		this.wireChannelCallbacks()
		this.wireConnectionStateHandlers(pc, sendCommand)

		// Gather ICE candidates and send them to hub via signaling WS
		pc.onicecandidate = (ev) => {
			if (ev.candidate) {
				sendCommand('rtc.ice', {
					candidate: {
						candidate: ev.candidate.candidate,
						sdpMid: ev.candidate.sdpMid,
						sdpMLineIndex: ev.candidate.sdpMLineIndex,
					},
				}).catch(() => {})
			}
		}

		// Create offer
		const offer = await pc.createOffer()
		await pc.setLocalDescription(offer)

		// Send offer and wait for answer
		const answerPromise = new Promise<RTCSessionDescriptionInit>(
			(resolve, reject) => {
				this.answerResolve = resolve
				this.answerReject = reject
			},
		)
		const timeoutPromise = new Promise<never>((_, reject) =>
			setTimeout(
				() => reject(new Error('rtc_answer_timeout')),
				WebRtcTransportService.CONNECT_TIMEOUT_MS,
			),
		)

		// Send the offer; the hub will reply with an ack containing the SDP answer
		const ack = await sendCommand('rtc.offer', {
			sdp: offer.sdp,
			type: offer.type,
		})

		console.log('🔍 rtc.offer ack:', ack)

		// The ack.data contains the answer SDP
		if (ack?.data?.sdp) {
			await pc.setRemoteDescription({
				type: ack.data.type || 'answer',
				sdp: ack.data.sdp,
			})
		} else {
			// Fall back to waiting for a separate rtc.answer message
			const answer = await Promise.race([answerPromise, timeoutPromise])
			await pc.setRemoteDescription(answer)
		}

		this.state$.next('connecting')

		// Wait for both DataChannels to open
		await Promise.race([
			this.waitForChannelsOpen(),
			new Promise<never>((_, reject) =>
				setTimeout(
					() => reject(new Error('dc_open_timeout')),
					WebRtcTransportService.CONNECT_TIMEOUT_MS,
				),
			),
		])

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
				this.onEventsMessage?.(
					typeof ev.data === 'string' ? ev.data : '',
				)
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
		let disconnectTimer: ReturnType<typeof setTimeout> | null = null

		pc.oniceconnectionstatechange = () => {
			const st = pc.iceConnectionState

			if (st === 'connected' || st === 'completed') {
				if (disconnectTimer) {
					clearTimeout(disconnectTimer)
					disconnectTimer = null
				}
				if (this.state$.value !== 'connected') {
					this.state$.next('connected')
				}
				return
			}

			if (st === 'disconnected') {
				// Brief disconnects are common (e.g. network switch). Wait before reacting.
				disconnectTimer = setTimeout(() => {
					if (pc.iceConnectionState === 'disconnected') {
						this.attemptIceRestart(pc, sendCommand)
					}
				}, WebRtcTransportService.DISCONNECT_GRACE_MS)
				return
			}

			if (st === 'failed') {
				if (disconnectTimer) {
					clearTimeout(disconnectTimer)
					disconnectTimer = null
				}
				this.attemptIceRestart(pc, sendCommand)
			}
		}
	}

	private async attemptIceRestart(
		pc: RTCPeerConnection,
		sendCommand: (
			kind: string,
			payload: Record<string, any>,
		) => Promise<any>,
	): Promise<void> {
		if (this.iceRestartCount >= WebRtcTransportService.ICE_RESTART_MAX) {
			this.state$.next('failed')
			return
		}
		this.iceRestartCount++
		this.state$.next('connecting')

		try {
			const offer = await pc.createOffer({ iceRestart: true })
			await pc.setLocalDescription(offer)

			const ack = await sendCommand('rtc.offer', {
				sdp: offer.sdp,
				type: offer.type,
			})
			if (ack?.data?.sdp) {
				await pc.setRemoteDescription({
					type: ack.data.type || 'answer',
					sdp: ack.data.sdp,
				})
			}
		} catch {
			this.state$.next('failed')
		}
	}

	// -- signaling WS listener ------------------------------------------------

	private installSignalingListener(ws: WebSocket): void {
		this.removeSignalingListener()
		const listener = (ev: MessageEvent) => {
			try {
				const msg = JSON.parse(ev.data)
				// Hub pushes ICE candidates as: { ch: "events", t: "evt", kind: "rtc.ice", payload: {...} }
				if (msg?.ch === 'events' && msg?.kind === 'rtc.ice') {
					const c = msg.payload?.candidate
					if (c && this.pc) {
						this.pc
							.addIceCandidate(new RTCIceCandidate(c))
							.catch(() => {})
					}
				}
				// Fallback: separate rtc.answer message (not used when answer is in ack.data)
				if (
					msg?.ch === 'events' &&
					msg?.kind === 'rtc.answer' &&
					msg?.payload?.sdp
				) {
					this.answerResolve?.({
						type: msg.payload.type || 'answer',
						sdp: msg.payload.sdp,
					})
				}
			} catch {
				// ignore non-json or irrelevant messages
			}
		}
		this.signalingListener = listener
		ws.addEventListener('message', listener)
	}

	private removeSignalingListener(): void {
		if (this.signalingWs && this.signalingListener) {
			this.signalingWs.removeEventListener(
				'message',
				this.signalingListener,
			)
		}
		this.signalingListener = null
		this.signalingWs = null
	}
}
