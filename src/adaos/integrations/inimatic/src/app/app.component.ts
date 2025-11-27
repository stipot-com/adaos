import { Component, OnDestroy, OnInit } from '@angular/core'
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
export class AppComponent implements OnInit, OnDestroy {
	isAndroid: boolean
	private colorSchemeMedia?: MediaQueryList
	private colorSchemeListener = (e: MediaQueryListEvent) => this.applyTheme(e.matches)

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

	ngOnInit(): void {
		this.colorSchemeMedia = window.matchMedia('(prefers-color-scheme: dark)')
		this.applyTheme(this.colorSchemeMedia.matches)
		this.colorSchemeMedia.addEventListener('change', this.colorSchemeListener)
	}

	ngOnDestroy(): void {
		this.colorSchemeMedia?.removeEventListener('change', this.colorSchemeListener)
	}

	private applyTheme(isDark: boolean): void {
		// Use both a generic "dark" flag and Ionic's palette class so that
		// Ionic web components (ion-card, ion-toolbar, ion-tab-bar, etc.)
		// actually switch to the dark color variables.
		document.body.classList.toggle('dark', isDark)
		document.body.classList.toggle('ion-palette-dark', isDark)
	}
}
