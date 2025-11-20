import { Component, EventEmitter, Output } from '@angular/core'
import { LoginService, type LoginResult } from './login.service'
import { IonButton, IonInput } from '@ionic/angular/standalone'
import { FormsModule } from '@angular/forms'
import { CommonModule } from '@angular/common'

@Component({
	selector: 'app-login',
	standalone: true,
	templateUrl: './login.component.html',
	styleUrl: './login.component.scss',
	imports: [IonInput, IonButton, FormsModule, CommonModule],
})
export class LoginComponent {
	@Output() loginSuccess = new EventEmitter<LoginResult>()

	deviceCode = ''
	loading = false
	errorMessage = ''

	constructor(private loginService: LoginService) {}

	onLogin() {
		this.errorMessage = ''

		if (!this.deviceCode.trim()) {
			this.errorMessage = 'Enter device code'
			return
		}

		this.loading = true

		this.loginService.login(this.deviceCode).subscribe({
			next: (result) => {
				this.loading = false
				this.loginSuccess.emit(result)
			},
			error: (err) => {
				this.loading = false

				if (err instanceof Error && !('status' in err)) {
					// Локальные ошибки (например, отсутствие поддержки WebAuthn)
					this.errorMessage = err.message || 'WebAuthn error'
					return
				}

				const status = err.status ?? 0
				if (status === 400) {
					this.errorMessage = 'Invalid login or WebAuthn registration required'
				} else if (status >= 500) {
					this.errorMessage = 'Server error'
				} else {
					this.errorMessage = 'Unexpected error'
				}
			},
		})
	}
}
