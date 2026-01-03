import { CommonModule } from '@angular/common'
import {
	AfterViewInit,
	Component,
	ElementRef,
	EventEmitter,
	OnDestroy,
	Output,
	ViewChild,
} from '@angular/core'
import { IonicModule } from '@ionic/angular'
import jsQR from 'jsqr-es6'

@Component({
	selector: 'ada-qr-scanner',
	standalone: true,
	imports: [CommonModule, IonicModule],
	template: `
		<div class="qr">
			<div class="controls">
				<ion-button size="small" (click)="start()" [disabled]="active">Start</ion-button>
				<ion-button size="small" color="medium" (click)="stop()" [disabled]="!active">Stop</ion-button>
				<input #file type="file" accept="image/*" (change)="onFile($event)" />
			</div>

			<video #video playsinline autoplay muted [style.display]="active ? 'block' : 'none'"></video>
			<canvas #canvas style="display:none;"></canvas>
		</div>
	`,
	styles: [
		`
			.qr {
				display: flex;
				flex-direction: column;
				gap: 10px;
				width: 100%;
			}
			.controls {
				display: flex;
				gap: 8px;
				align-items: center;
				flex-wrap: wrap;
			}
			video {
				width: min(100%, 520px);
				border-radius: 12px;
			}
			input[type='file'] {
				max-width: 100%;
			}
		`,
	],
})
export class QrScannerComponent implements AfterViewInit, OnDestroy {
	@Output() scan = new EventEmitter<string>()
	@ViewChild('video', { static: true }) videoRef!: ElementRef<HTMLVideoElement>
	@ViewChild('canvas', { static: true }) canvasRef!: ElementRef<HTMLCanvasElement>

	active = false
	private stream?: MediaStream
	private rafId = 0

	ngAfterViewInit(): void {
		// no-op (video/canvas refs ready)
	}

	ngOnDestroy(): void {
		this.stop()
	}

	async start(): Promise<void> {
		if (this.active) return
		this.active = true
		try {
			this.stream = await navigator.mediaDevices.getUserMedia({
				video: { facingMode: 'environment' },
				audio: false,
			})
			const video = this.videoRef.nativeElement
			video.srcObject = this.stream
			await video.play()
			this.loop()
		} catch {
			this.active = false
			this.stop()
		}
	}

	stop(): void {
		this.active = false
		if (this.rafId) cancelAnimationFrame(this.rafId)
		this.rafId = 0
		try {
			this.videoRef.nativeElement.pause()
			this.videoRef.nativeElement.srcObject = null
		} catch {}
		try {
			this.stream?.getTracks().forEach((t) => t.stop())
		} catch {}
		this.stream = undefined
	}

	private loop(): void {
		if (!this.active) return
		this.rafId = requestAnimationFrame(() => this.loop())
		const video = this.videoRef.nativeElement
		if (!video.videoWidth || !video.videoHeight) return
		const canvas = this.canvasRef.nativeElement
		const ctx = canvas.getContext('2d')
		if (!ctx) return
		canvas.width = video.videoWidth
		canvas.height = video.videoHeight
		ctx.drawImage(video, 0, 0, canvas.width, canvas.height)
		const img = ctx.getImageData(0, 0, canvas.width, canvas.height)
		const code = jsQR(img.data, img.width, img.height)
		if (code?.data) {
			this.scan.emit(code.data)
			this.stop()
		}
	}

	async onFile(ev: Event): Promise<void> {
		const input = ev.target as HTMLInputElement
		const file = input.files?.[0]
		if (!file) return
		try {
			const img = await this.loadImageFromFile(file)
			const canvas = this.canvasRef.nativeElement
			const ctx = canvas.getContext('2d')
			if (!ctx) return
			canvas.width = img.naturalWidth || img.width
			canvas.height = img.naturalHeight || img.height
			ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
			const data = ctx.getImageData(0, 0, canvas.width, canvas.height)
			const code = jsQR(data.data, data.width, data.height)
			if (code?.data) this.scan.emit(code.data)
		} finally {
			try {
				input.value = ''
			} catch {}
		}
	}

	private loadImageFromFile(file: File): Promise<HTMLImageElement> {
		return new Promise((resolve, reject) => {
			const url = URL.createObjectURL(file)
			const img = new Image()
			img.onload = () => {
				URL.revokeObjectURL(url)
				resolve(img)
			}
			img.onerror = () => {
				URL.revokeObjectURL(url)
				reject(new Error('image load failed'))
			}
			img.src = url
		})
	}
}

