import { Component, EventEmitter, Output } from '@angular/core'
import { ActivatedRoute } from '@angular/router'
import { LoginService, type LoginResult } from './login.service'
import { IonButton, IonInput } from '@ionic/angular/standalone'
import { FormsModule } from '@angular/forms'
import { CommonModule } from '@angular/common'
import { I18nService } from '../../runtime/i18n.service'
import { TPipe } from '../../runtime/t.pipe'
import { LanguagePickerComponent } from '../../shared/i18n/language-picker.component'

@Component({
	selector: 'app-login',
	standalone: true,
	templateUrl: './login.component.html',
	styleUrl: './login.component.scss',
	imports: [IonInput, IonButton, FormsModule, CommonModule, TPipe, LanguagePickerComponent],
})
export class LoginComponent {
	@Output() loginSuccess = new EventEmitter<LoginResult>()

	// Registration mode
	userCode = ''
	registrationLoading = false
	registrationErrorKey = ''
	registrationErrorParams: Record<string, any> | undefined

	// Login mode
	loginLoading = false
	loginErrorKey = ''
	loginErrorParams: Record<string, any> | undefined

	// Mode toggle
	mode: 'selection' | 'registration' | 'login' = 'selection'

	constructor(
		private loginService: LoginService,
		private route: ActivatedRoute,
		public i18n: I18nService,
	) {
		this.route.queryParamMap.subscribe((params) => {
			const mode = (params.get('mode') || '').trim().toLowerCase()
			const code = (params.get('user_code') || params.get('code') || '').trim()
			if (mode === 'registration') {
				this.mode = 'registration'
			} else if (mode === 'login') {
				this.mode = 'login'
			}
			if (code) {
				this.mode = 'registration'
				this.userCode = code
			}
		})
	}

	switchToRegistration() {
		this.mode = 'registration'
		this.registrationErrorKey = ''
		this.registrationErrorParams = undefined
		this.userCode = ''
	}

	switchToLogin() {
		this.mode = 'login'
		this.loginErrorKey = ''
		this.loginErrorParams = undefined
	}

	backToSelection() {
		this.mode = 'selection'
		this.registrationErrorKey = ''
		this.registrationErrorParams = undefined
		this.loginErrorKey = ''
		this.loginErrorParams = undefined
	}

	onRegister() {
		this.registrationErrorKey = ''
		this.registrationErrorParams = undefined

		if (!this.userCode.trim()) {
			this.registrationErrorKey = 'login.error.enter_code'
			return
		}

		this.registrationLoading = true

		this.loginService.register(this.userCode).subscribe({
			next: (result) => {
				this.registrationLoading = false
				this.loginSuccess.emit(result)
			},
			error: (err) => {
				this.registrationLoading = false

				if (err instanceof Error && !('status' in err)) {
					// Локальные ошибки (например, отсутствие поддержки WebAuthn)
					this.registrationErrorKey = 'login.error.webauthn'
					this.registrationErrorParams = { msg: err.message || '' }
					return
				}

				const status = err.status ?? 0
				if (status === 400) {
					this.registrationErrorKey = 'login.error.invalid_code'
				} else if (status >= 500) {
					this.registrationErrorKey = 'login.error.server'
				} else {
					this.registrationErrorKey = 'login.error.unexpected'
				}
			},
		})
	}

	onLogin() {
		this.loginErrorKey = ''
		this.loginErrorParams = undefined
		this.loginLoading = true

		this.loginService.login().subscribe({
			next: (result) => {
				this.loginLoading = false
				this.loginSuccess.emit(result)
			},
			error: (err) => {
				this.loginLoading = false

				if (err instanceof Error && !('status' in err)) {
					// Локальные ошибки (например, отсутствие поддержки WebAuthn)
					this.loginErrorKey = 'login.error.webauthn'
					this.loginErrorParams = { msg: err.message || '' }
					return
				}

				const status = err.status ?? 0
				if (status === 400) {
					const errorCode = err.error?.code || err.error?.error
					if (errorCode === 'no_credentials_registered') {
						this.loginErrorKey = 'login.error.no_credentials'
					} else {
						this.loginErrorKey = 'login.error.auth_failed'
					}
				} else if (status >= 500) {
					this.loginErrorKey = 'login.error.server'
				} else {
					this.loginErrorKey = 'login.error.unexpected'
				}
			},
		})
	}
}
