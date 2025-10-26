import { Component, EventEmitter, Output } from '@angular/core'
import { LoginService } from './login.service'
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
	@Output() loginSuccess = new EventEmitter<void>()

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
			next: () => {
				this.loading = false
				this.loginSuccess.emit()
			},
			error: (err) => {
				this.loading = false

				if (err.status === 400) {
					this.errorMessage = 'Invalid login'
				} else if (err.status >= 500) {
					this.errorMessage = 'Server error'
				} else {
					this.errorMessage = 'Unexpected error'
				}
			},
		})
	}
}
