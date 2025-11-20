import { Component, ChangeDetectorRef } from '@angular/core'
import { CommonModule } from '@angular/common'
import { FormsModule } from '@angular/forms' // Make sure this import is included
import { Platform } from '@ionic/angular'
import { addIcons } from 'ionicons'
import { close } from 'ionicons/icons'
import {
	IonContent,
	IonButton,
	IonCard,
	IonCardTitle,
	IonIcon,
	IonInput,
} from '@ionic/angular/standalone'
import { QRCodeModule } from 'angularx-qrcode'
import { io, Socket } from 'socket.io-client'
import { environment } from 'src/environments/environment'
import streamSaver from 'streamsaver'
import {
	Data,
	TransferFileData,
	SendMessageData,
	ConfirmationData,
} from 'src/types'

@Component({
	selector: 'app-distribution',
	templateUrl: './public-point.component.html',
	styleUrls: ['./public-point.component.scss'],
	standalone: true,
	imports: [
		IonInput,
		IonIcon,
		IonCardTitle,
		IonCard,
		IonContent,
		QRCodeModule,
		FormsModule,
		CommonModule,
		IonButton,
	],
})
export class PublicPointComponent {
	sessionID = '-'
	url = location.origin + '/follower'
	qrData = ''
	isInitiator = true
	isConnected = false
	followers: string[] = []
	message = ''
	socket: Socket
	file: File | null = null
	fileList: string[] = []
	writableStream: WritableStream | null = null
	writer: WritableStreamDefaultWriter<any> | null = null
	fileData: Data | null = null
	messagesLog: string[] = []

	constructor(private plt: Platform, private cdr: ChangeDetectorRef) {
		addIcons({ close })
		const isInStandaloneMode = () =>
			'standalone' in window.navigator && window.navigator['standalone']

		if (this.plt.is('ios') && isInStandaloneMode()) {
			console.log('I am a an iOS PWA!')
			// E.g. hide the scan functionality!
		}

		this.socket = io(environment.app_server_url, { secure: true })

		this.isInitiator = location.pathname !== '/follower'

		this.socket.emit('add_initiator', 'public')

		this.socket.on('session_id', (data) => {
			this.sessionID = data
			this.qrData =
				this.url + `?sessionId=${this.sessionID}` + '&isPublicMode=1'
		})

		this.socket.on('connect_follower', async (data) => {
			this.followers.push(data)
			this.showConnectedStage()
			console.log(data)
		})

		this.socket.on('follower_disconnect', async (follower) => {
			this.followers = this.followers.filter(
				(followerName) => followerName !== follower
			)

			if (!this.followers.length) {
				this.isConnected = false
			}

			if (this.writer) {
				this.writer.releaseLock()
				this.writer = null
			}

			if (this.writableStream) {
				await this.writableStream.abort()
				this.writableStream = null
			}

			this.cdr.detectChanges()
		})

		this.socket.on('connection', (data, fn) => {
			this.receiveData(data)

			if (fn) {
				fn()
			}
		})

		this.socket.on('saved_file', (fileName) => {
			this.fileList.push(fileName)
			console.log(this.fileList)
		})
	}

	async send(data: any) {
		if (!this.isConnected) {
			console.log('Peer not connected.')
			return
		}
		console.log('send')

		await new Promise<void>((resolve) => {
			this.socket.emit(
				'conductor',
				{
					sessionId: this.sessionID,
					isInitiator: this.isInitiator,
					data: data,
				},
				() => resolve()
			)
		})
	}

	async connect() {
		this.socket.emit('conductor', {
			sessionId: this.sessionID,
			isInitiator: this.isInitiator,
			data: 'connect',
		})
	}

	showConnectedStage() {
		this.isConnected = true
		console.log('CONNECT')
		this.cdr.detectChanges()
	}

	disconnectDevice(followerName: string) {
		this.socket.emit('disconnect_follower', {
			sessionId: this.sessionID,
			isInitiator: this.isInitiator,
			followerName: followerName,
		})
	}

	sendMessage() {
		this.send({ type: 'sendMessage', message: this.message })
	}

	uploadFile(event: Event) {
		const target = event.target as HTMLInputElement
		if (!target.files || target.files.length === 0) return
		this.file = target.files[0]
	}

	async transferFile() {
		await this.send({
			type: 'transferFile',
			fileName: this.file?.name,
			size: this.file?.size,
		})

		const chunksize = 64 * 1024
		let offset = 0
		while (offset < this.file!.size) {
			const chunkfile = this.file!.slice(offset, offset + chunksize)
			const chunk = await chunkfile.arrayBuffer()
			await this.sendChunk(new Uint8Array(chunk))
			offset += chunksize
		}

		await this.send({
			type: 'transferFile',
			fileName: this.file!.name,
			size: this.file!.size,
			end: true,
		})
	}

	async sendChunk(value: Uint8Array) {
		await this.send({
			type: 'transferFile',
			fileName: this.file!.name,
			size: this.file!.size,
			content: Array.from(value),
		})
	}

	receiveData(data: any) {
		const receivedData: Data = data

		if (receivedData.type === 'transferFile') {
			this.receiveFile(receivedData)
		} else if (receivedData.type === 'sendMessage') {
			this.receiveMessage(receivedData)
		} else if (receivedData.type === 'confirmation') {
			this.receiveConfirmationData(receivedData)
		}
	}

	receiveFile(receivedData: TransferFileData) {
		if (this.writableStream === null) {
			this.writableStream = streamSaver.createWriteStream(
				receivedData.fileName,
				{
					size: receivedData.size,
				}
			)
			this.writer = this.writableStream.getWriter()
			return
		}

		if (receivedData.end) {
			console.log('end')
			this.writer!.close()
			this.writableStream = null
			this.writer = null
			return
		}

		this.writer!.write(new Uint8Array(receivedData.content!))
	}

	receiveMessage(receivedData: SendMessageData) {
		console.log(receivedData.message)
		this.messagesLog = this.messagesLog.concat([receivedData.message])
	}

	receiveConfirmationData(receivedData: ConfirmationData) {
		if (receivedData.confirmed) {
			this.showConnectedStage()
		}
	}
}
