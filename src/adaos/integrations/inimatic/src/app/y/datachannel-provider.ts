/**
 * Yjs sync provider that operates over a WebRTC DataChannel instead of a
 * WebSocket.  Implements the same binary protocol as y-websocket so the
 * server-side ypy-websocket can serve it transparently.
 *
 * Usage mirrors WebsocketProvider from y-websocket:
 *
 *   const provider = new DataChannelProvider(doc, dataChannel)
 *   provider.on('sync', (synced: boolean) => { ... })
 *   provider.destroy()
 */

import * as Y from 'yjs'
import * as syncProtocol from 'y-protocols/sync'
import * as awarenessProtocol from 'y-protocols/awareness'
import * as encoding from 'lib0/encoding'
import * as decoding from 'lib0/decoding'
import { Observable } from 'lib0/observable'

const MSG_SYNC = 0
const MSG_AWARENESS = 1

export class DataChannelProvider extends Observable<string> {
	synced = false
	awareness: awarenessProtocol.Awareness
	private dc: RTCDataChannel
	private _destroyed = false

	constructor(
		public doc: Y.Doc,
		dc: RTCDataChannel,
	) {
		super()
		this.dc = dc
		this.awareness = new awarenessProtocol.Awareness(doc)
		this.dc.binaryType = 'arraybuffer'

		this.dc.onmessage = (ev: MessageEvent) => {
			if (this._destroyed) return
			const data = new Uint8Array(ev.data)
			this.handleMessage(data)
		}

		this.dc.onclose = () => {
			if (!this._destroyed) {
				this.synced = false
				this.emit('sync', [false])
			}
		}

		// Listen for local doc updates
		this.doc.on('update', this.onDocUpdate)
		this.awareness.on('update', this.onAwarenessUpdate)

		// Kick off sync when channel is open
		if (dc.readyState === 'open') {
			this.sendSyncStep1()
		} else {
			dc.onopen = () => this.sendSyncStep1()
		}
	}

	private sendSyncStep1(): void {
		const encoder = encoding.createEncoder()
		encoding.writeVarUint(encoder, MSG_SYNC)
		syncProtocol.writeSyncStep1(encoder, this.doc)
		this.send(encoding.toUint8Array(encoder))

		// Also broadcast current awareness state
		const awarenessEncoder = encoding.createEncoder()
		encoding.writeVarUint(awarenessEncoder, MSG_AWARENESS)
		encoding.writeVarUint8Array(
			awarenessEncoder,
			awarenessProtocol.encodeAwarenessUpdate(this.awareness, [
				this.doc.clientID,
			]),
		)
		this.send(encoding.toUint8Array(awarenessEncoder))
	}

	private handleMessage(data: Uint8Array): void {
		const decoder = decoding.createDecoder(data)
		const msgType = decoding.readVarUint(decoder)

		if (msgType === MSG_SYNC) {
			const encoder = encoding.createEncoder()
			encoding.writeVarUint(encoder, MSG_SYNC)
			const syncMessageType = syncProtocol.readSyncMessage(
				decoder,
				encoder,
				this.doc,
				this,
			)
			if (encoding.length(encoder) > 1) {
				this.send(encoding.toUint8Array(encoder))
			}
			// syncMessageType 1 = SyncStep2 (initial state received)
			if (syncMessageType === 1 && !this.synced) {
				this.synced = true
				this.emit('sync', [true])
			}
		} else if (msgType === MSG_AWARENESS) {
			awarenessProtocol.applyAwarenessUpdate(
				this.awareness,
				decoding.readVarUint8Array(decoder),
				this,
			)
		}
	}

	private onDocUpdate = (update: Uint8Array, origin: any): void => {
		if (origin === this || this._destroyed) return
		const encoder = encoding.createEncoder()
		encoding.writeVarUint(encoder, MSG_SYNC)
		syncProtocol.writeUpdate(encoder, update)
		this.send(encoding.toUint8Array(encoder))
	}

	private onAwarenessUpdate = ({ added, updated, removed }: any): void => {
		if (this._destroyed) return
		const changedClients = (added || [])
			.concat(updated || [])
			.concat(removed || [])
		const encoder = encoding.createEncoder()
		encoding.writeVarUint(encoder, MSG_AWARENESS)
		encoding.writeVarUint8Array(
			encoder,
			awarenessProtocol.encodeAwarenessUpdate(
				this.awareness,
				changedClients,
			),
		)
		this.send(encoding.toUint8Array(encoder))
	}

	private send(data: Uint8Array): void {
		if (this.dc.readyState === 'open') {
			this.dc.send(data)
		}
	}

	override destroy(): void {
		this._destroyed = true
		this.doc.off('update', this.onDocUpdate)
		this.awareness.off('update', this.onAwarenessUpdate)
		awarenessProtocol.removeAwarenessStates(
			this.awareness,
			[this.doc.clientID],
			this,
		)
		super.destroy()
	}
}
