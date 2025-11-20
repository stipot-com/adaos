import { Component, ChangeDetectorRef } from '@angular/core'
import { CommonModule } from '@angular/common'
import { FormsModule } from '@angular/forms' // Make sure this import is included
import { Platform } from '@ionic/angular'
import { addIcons } from 'ionicons'
import { close, camera, refresh } from 'ionicons/icons'
import { IonContent, IonButton, IonInput } from '@ionic/angular/standalone'
import { QRCodeModule } from 'angularx-qrcode'
import { io, Socket } from 'socket.io-client'
import { environment } from 'src/environments/environment'
import streamSaver from 'streamsaver'
import {
	Data,
	TransferFileData,
	SendMessageData,
	VerificationData,
	ConfirmationData,
	CookieData,
} from 'src/types'

@Component({
	selector: 'app-home',
	templateUrl: 'private-point.page.html',
	styleUrls: ['private-point.page.scss'],
	standalone: true,
	imports: [
		IonInput,
		IonContent,
		QRCodeModule,
		FormsModule,
		CommonModule,
		IonButton,
	],
})
export class PrivatePointPage {
	sessionID = '-'
	url = location.origin + '/follower'
	qrData = ''
	isInitiator = true
	verificationStep = false
	isConnected = false
	followers: string[] = []
	message = ''
	socket: Socket
	verificationImage = ''
	file: File | null = null
	writableStream: WritableStream | null = null
	writer: WritableStreamDefaultWriter<any> | null = null
	fileData: Data | null = null
	messagesLog: string[] = []

	constructor(private plt: Platform, private cdr: ChangeDetectorRef) {
		addIcons({ close, camera, refresh })

		this.socket = io(environment.app_server_url, { secure: true })

		this.isInitiator = location.pathname !== '/follower'

		this.socket.emit('add_initiator', 'private')

		this.socket.on('session_id', (data) => {
			this.sessionID = data
			this.qrData = this.url + `?sessionId=${this.sessionID}`
		})

		this.socket.on('connect_follower', async (data) => {
			this.followers.push(data)
			this.verificationStep = false
			this.verificationImage = ''
			this.showConnectedStage()
			console.log(data)
		})

		this.socket.on('follower_disconnect', async (follower) => {
			if (this.verificationStep) {
				this.verificationStep = false
			}

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

		this.socket.on('connection', (data) => {
			this.receiveData(data)
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

	test() {
		console.log(this.message)
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
		} else if (receivedData.type === 'verify') {
			this.receiveVerificationImage(receivedData)
		} else if (receivedData.type === 'transferCookies') {
			this.receiveCookies(receivedData)
		}

		// check is it necessary
		// else if (receivedData.type === 'confirmation') {
		// 	this.receiveConfirmationData(receivedData)
		// }
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

	receiveVerificationImage(receivedData: VerificationData) {
		this.verificationStep = true
		this.verificationImage = receivedData.content
		this.cdr.detectChanges()
	}

	receiveConfirmationData(receivedData: ConfirmationData) {
		if (receivedData.confirmed) {
			this.showConnectedStage()
		}
	}

	receiveCookies(receiveData: CookieData) {
		window.postMessage(
			{
				type: 'set_session',
				body: {
					url: receiveData.url,
					cookies: receiveData.cookies,
				},
			},
			'*'
		)
	}
}
