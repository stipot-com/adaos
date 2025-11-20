import { Component, OnInit } from '@angular/core'
import { LoginComponent } from '../features/login/login.component'
import { CommonModule } from '@angular/common'

@Component({
	selector: 'app-desktop',
	standalone: true,
	templateUrl: './desktop.component.html',
	styleUrls: ['./desktop.component.scss'],
	imports: [LoginComponent, CommonModule],
})
export class DesktopComponent implements OnInit {
	isAuthenticated = false

	constructor() {}

	ngOnInit() {}

	onLoginSuccess(): void {
		// На этом этапе у нас уже есть sessionJwt и sid внутри LoginService.
		// Здесь можно будет инициировать подключение к WebSocket /owner и загрузку рабочего стола.
		this.isAuthenticated = true
	}
}
