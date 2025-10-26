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
	constructor() {}

	ngOnInit() {}

	onLoginSuccess() {
		return
	}
}
