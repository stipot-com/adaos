import { Component, HostListener, OnDestroy, OnInit } from '@angular/core'
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
	IonButtons,
	IonButton,
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
	homeOutline,
	refreshOutline,
} from 'ionicons/icons'
import { Platform } from '@ionic/angular'
import { YDocService } from './y/ydoc.service'
import { AdaosClient } from './core/adaos/adaos-client.service'

@Component({
	selector: 'app-root',
	templateUrl: 'app.component.html',
	styleUrls: ['app.component.scss'],
	standalone: true,
		imports: [
		IonIcon,
		IonToolbar,
		IonTitle,
		IonButtons,
		IonButton,
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

	constructor(private plt: Platform, private ydoc: YDocService, private adaos: AdaosClient) {
		addIcons({
			lockClosedOutline,
			people,
			phonePortrait,
			settings,
			apps,
			laptop,
			desktop,
			homeOutline,
			refreshOutline,
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

	@HostListener('window:keydown', ['$event'])
	onKeyDown(ev: KeyboardEvent): void {
		if (ev.altKey && (ev.key === 'w' || ev.key === 'W')) {
			try {
				this.ydoc.dumpSnapshot()
			} catch {
				// ignore debug errors
			}
		}
	}

	private applyTheme(isDark: boolean): void {
		// Use both a generic "dark" flag and Ionic's palette class so that
		// Ionic web components (ion-card, ion-toolbar, ion-tab-bar, etc.)
		// actually switch to the dark color variables.
		document.body.classList.toggle('dark', isDark)
		document.body.classList.toggle('ion-palette-dark', isDark)
	}

	async onClickHome(): Promise<void> {
		try {
			// Switch current scenario back to web_desktop for the active webspace.
			await this.adaos.sendEventsCommand('desktop.scenario.set', {
				scenario_id: 'web_desktop',
			})
		} catch (err) {
			// best-effort only; errors can be inspected in console
			// eslint-disable-next-line no-console
			console.warn('desktop.scenario.set failed', err)
		}
	}

	async onClickYjsReload(): Promise<void> {
		try {
			const ws = this.ydoc.getWebspaceId()
			await this.adaos.post('/api/yjs/reload', {
				webspace_id: ws || 'default',
			}).toPromise()
		} catch (err) {
			// eslint-disable-next-line no-console
			console.warn('YJS reload failed', err)
		}
	}
}
