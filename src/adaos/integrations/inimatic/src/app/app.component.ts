import { Component, HostListener, NgZone, OnDestroy, OnInit } from '@angular/core'
import {
	IonApp,
	IonHeader,
	IonTitle,
	IonToolbar,
	IonIcon,
	IonButtons,
	IonButton,
} from '@ionic/angular/standalone'
import { addIcons } from 'ionicons'
import {
	homeOutline,
	refreshOutline,
	logOutOutline,
	menuOutline,
	closeOutline,
	chevronDownOutline,
	chevronUpOutline,
	folderOpenOutline,
	micOutline,
} from 'ionicons/icons'
import { Platform } from '@ionic/angular'
import { YDocService } from './y/ydoc.service'
import { AdaosClient } from './core/adaos/adaos-client.service'
import { CommonModule } from '@angular/common'
import { Observable, of, timer } from 'rxjs'
import { catchError, distinctUntilChanged, map, startWith, switchMap, timeout } from 'rxjs/operators'
import { buildId } from '../environments/build'
import { HttpClient, HttpHeaders } from '@angular/common/http'
import { PairingService } from './runtime/pairing.service'
import { IonRouterOutlet } from '@ionic/angular/standalone'
import { TPipe } from './runtime/t.pipe'
import { HttpErrorResponse } from '@angular/common/http'

@Component({
	selector: 'app-root',
	templateUrl: 'app.component.html',
	styleUrls: ['app.component.scss'],
	standalone: true,
	imports: [
		CommonModule,
		IonIcon,
		IonToolbar,
		IonTitle,
		IonButtons,
		IonButton,
		IonHeader,
		IonApp,
		IonRouterOutlet,
		TPipe,
	],
})
export class AppComponent implements OnInit, OnDestroy {
	isAndroid: boolean
	hubStatus$!: Observable<'checking' | 'online' | 'offline'>
	readonly buildId = buildId
	logoSrc = 'assets/icon/favicon.svg'
	private colorSchemeMedia?: MediaQueryList
	private colorSchemeListener = (e: MediaQueryListEvent) => this.applyTheme(e.matches)
	private narrowMedia?: MediaQueryList
	private narrowListener = () => this.applyNarrow()
	isNarrow = false
	private sessionInvalidated = false
	sidebarAvailable = false
	private sidebarAvailabilityHandler = (ev: any) => {
		try {
			const available = !!ev?.detail?.available
			this.zone.run(() => {
				this.sidebarAvailable = available
			})
		} catch { }
	}

	constructor(
		private plt: Platform,
		private ydoc: YDocService,
		private adaos: AdaosClient,
		private http: HttpClient,
		private pairing: PairingService,
		private zone: NgZone,
	) {
		addIcons({
			homeOutline,
			refreshOutline,
			logOutOutline,
			menuOutline,
			closeOutline,
			chevronDownOutline,
			chevronUpOutline,
			folderOpenOutline,
			micOutline,
		})
		this.isAndroid =
			this.plt.platforms().includes('mobile') &&
			!this.plt.platforms().includes('mobileweb')
	}

	ngOnInit(): void {
		this.applyLayoutVars()
		this.maybeHardReloadOnBuildChange()
		this.ensureNarrowMedia()
		this.colorSchemeMedia = window.matchMedia('(prefers-color-scheme: dark)')
		this.applyTheme(this.colorSchemeMedia.matches)
		this.colorSchemeMedia.addEventListener('change', this.colorSchemeListener)
		try {
			window.addEventListener('adaos:sidebarAvailability', this.sidebarAvailabilityHandler as any)
		} catch { }
		// If we loaded via cache-bust URL, clean it up for nicer sharing/bookmarks.
		setTimeout(() => this.stripVersionParam(), 0)

		this.hubStatus$ = timer(0, 5000).pipe(
			switchMap(() => {
				const { url, headers } = this.getHubStatusRequest()
				return this.http.get(url, { responseType: 'text', headers }).pipe(
					timeout(2000),
					map(() => 'online' as const),
					catchError((err) => {
						// Root-proxy returns 401/403 when the stored session_jwt is expired/invalid.
						try {
							const httpErr = err as HttpErrorResponse
							if ((httpErr?.status === 401 || httpErr?.status === 403) && !this.sessionInvalidated) {
								this.sessionInvalidated = true
								this.invalidateOwnerSessionAndReload()
							}
						} catch { }
						return of('offline' as const)
					}),
				)
			}),
			startWith('checking' as const),
			distinctUntilChanged(),
		)
	}

	private getHubStatusRequest(): { url: string; headers?: HttpHeaders } {
		const base = this.pairing.getBaseUrl().replace(/\/+$/, '')
		const hubId = (() => {
			try {
				return (localStorage.getItem('adaos_hub_id') || '').trim()
			} catch {
				return ''
			}
		})()
		const jwt = this.readSessionJwt()
		if (hubId && jwt) {
			return {
				url: `${base}/hubs/${encodeURIComponent(hubId)}/api/node/status`,
				headers: new HttpHeaders({ Authorization: `Bearer ${jwt}` }),
			}
		}
		// Before pairing/login we don't know a hub id; root health is still useful.
		// Root-proxy may not expose `/healthz` but does expose `/api/ping`.
		return { url: `${base}/api/ping` }
	}

	private readSessionJwt(): string {
		try {
			return (localStorage.getItem('adaos_web_session_jwt') || '').trim()
		} catch {
			return ''
		}
	}

	private invalidateOwnerSessionAndReload(): void {
		const keys = [
			'adaos_web_session_jwt',
			'adaos_web_sid',
			'adaos_hub_id',
		]
		for (const key of keys) {
			try {
				localStorage.removeItem(key)
			} catch { }
		}
		try {
			location.reload()
		} catch { }
	}

	ngOnDestroy(): void {
		this.colorSchemeMedia?.removeEventListener('change', this.colorSchemeListener)
		try {
			window.removeEventListener('adaos:sidebarAvailability', this.sidebarAvailabilityHandler as any)
		} catch { }
		try {
			const any: any = this.narrowMedia as any
			if (typeof any?.removeEventListener === 'function') {
				this.narrowMedia?.removeEventListener('change', this.narrowListener as any)
			} else if (typeof any?.removeListener === 'function') {
				any.removeListener(this.narrowListener)
			}
		} catch { }
	}

	private applyLayoutVars(): void {
		try {
			const h = this.isAndroid ? '80px' : '56px'
			document.documentElement.style.setProperty('--ada-app-header-height', h)
		} catch { }
	}

	private ensureNarrowMedia(): void {
		try {
			this.narrowMedia = window.matchMedia('(max-width: 900px)')
			this.applyNarrow()
			const any: any = this.narrowMedia as any
			if (typeof any?.addEventListener === 'function') {
				this.narrowMedia.addEventListener('change', this.narrowListener as any)
			} else if (typeof any?.addListener === 'function') {
				any.addListener(this.narrowListener)
			}
		} catch { }
	}

	private applyNarrow(): void {
		try {
			this.isNarrow = !!this.narrowMedia?.matches
		} catch {
			this.isNarrow = false
		}
	}

	toggleSidebar(): void {
		try {
			window.dispatchEvent(new CustomEvent('adaos:toggleSidebar'))
		} catch { }
	}

	private maybeHardReloadOnBuildChange(): void {
		if (!this.buildId || this.buildId === 'dev') return
		const key = 'adaos_frontend_build'
		const guardKey = `adaos_frontend_reloaded_${this.buildId}`
		try {
			if (sessionStorage.getItem(guardKey) === '1') return
		} catch { }
		const prev = (() => {
			try {
				return (localStorage.getItem(key) || '').trim()
			} catch {
				return ''
			}
		})()
		if (!prev) {
			try {
				localStorage.setItem(key, this.buildId)
			} catch { }
			return
		}
		if (prev === this.buildId) return
		try {
			localStorage.setItem(key, this.buildId)
		} catch { }
		try {
			sessionStorage.setItem(guardKey, '1')
		} catch { }
		; (async () => {
			try {
				const regs = await navigator.serviceWorker?.getRegistrations?.()
				if (Array.isArray(regs)) {
					await Promise.all(regs.map((r) => r.unregister().catch(() => false)))
				}
			} catch { }
			try {
				const cacheKeys = await (globalThis as any).caches?.keys?.()
				if (Array.isArray(cacheKeys)) {
					await Promise.all(
						cacheKeys.map((k: string) => (globalThis as any).caches.delete(k).catch(() => false)),
					)
				}
			} catch { }
			try {
				const url = new URL(location.href)
				url.searchParams.set('__v', this.buildId)
				location.replace(url.toString())
				return
			} catch { }
			try {
				location.reload()
			} catch { }
		})()
	}

	stripVersionParam(): void {
		try {
			const url = new URL(location.href)
			if (!url.searchParams.has('__v')) return
			url.searchParams.delete('__v')
			window.history.replaceState({}, '', url.toString())
		} catch { }
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
		this.logoSrc = isDark ? 'assets/icon/favicon_dark.png' : 'assets/icon/favicon.png'
	}

	get isAuthenticated(): boolean {
		return Boolean(this.readSessionJwt())
	}

	async onClickHome(): Promise<void> {
		try {
			const ws = this.ydoc.getWebspaceId()
			// Switch current scenario back to web_desktop for the active webspace.
			await this.adaos.sendEventsCommand('desktop.scenario.set', {
				scenario_id: 'web_desktop',
				webspace_id: ws || undefined,
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

	onClickLogout(): void {
		// Debug-only: clear persisted auth/session so we can re-run onboarding easily.
		const keys = [
			'adaos_web_session_jwt',
			'adaos_hub_id',
			'adaos_web_sid',
			'adaos_hub_base',
		]
		for (const key of keys) {
			try {
				localStorage.removeItem(key)
			} catch { }
		}
		try {
			location.reload()
		} catch { }
	}
}
