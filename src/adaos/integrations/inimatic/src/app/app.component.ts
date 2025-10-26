import { Component } from '@angular/core'
import { HubComponent } from './features/hub/hub.component'
import {
	IonApp,
	IonRouterOutlet,
	IonTabButton,
	IonTabs,
	IonTabBar,
	IonHeader,
	IonTitle,
	IonToolbar,
	IonIcon,
} from '@ionic/angular/standalone'
import { addIcons } from 'ionicons'
import {
	apps,
	laptop,
	lockClosedOutline,
	people,
	phonePortrait,
	settings,
	desktop,
} from 'ionicons/icons'
import { Platform } from '@ionic/angular'

@Component({
	selector: 'app-root',
	templateUrl: 'app.component.html',
	styleUrls: ['app.component.scss'],
	standalone: true,
	imports: [
		IonIcon,
		IonToolbar,
		IonTitle,
		IonHeader,
		IonTabBar,
		IonTabs,
		IonTabButton,
		IonApp,
	],
})
export class AppComponent {
	isAndroid: boolean
	constructor(private plt: Platform) {
		addIcons({
			lockClosedOutline,
			people,
			phonePortrait,
			settings,
			apps,
			laptop,
			desktop,
		})
		this.isAndroid =
			this.plt.platforms().includes('mobile') &&
			!this.plt.platforms().includes('mobileweb')
	}
}
