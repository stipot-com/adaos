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
import { Platform } from '@ionic/angular'
import { YDocService } from './y/ydoc.service'
import { AdaosClient } from './core/adaos/adaos-client.service'
import { CommonModule } from '@angular/common'
import { Observable, combineLatest, of, timer, Subscription } from 'rxjs'
import { catchError, distinctUntilChanged, filter, map, pairwise, startWith, switchMap, timeout } from 'rxjs/operators'
import { buildId } from '../environments/build'
import { HttpClient, HttpHeaders } from '@angular/common/http'
import { PairingService } from './runtime/pairing.service'
import { HubMemberChannelsService } from './core/adaos/hub-member-channels.service'
import { ToastController } from '@ionic/angular/standalone'
import { IonRouterOutlet } from '@ionic/angular/standalone'
import { TPipe } from './runtime/t.pipe'
import { HttpErrorResponse } from '@angular/common/http'
import { observeDeep } from './y/y-helpers'

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
	transportState$!: Observable<string>
	readonly buildId = buildId
	logoSrc = 'assets/icon/favicon.svg'
	currentScenario = 'web_desktop'
	private colorSchemeMedia?: MediaQueryList
	private colorSchemeListener = (e: MediaQueryListEvent) => this.applyTheme(e.matches)
	private narrowMedia?: MediaQueryList
	private narrowListener = () => this.applyNarrow()
	isNarrow = false
	private sessionInvalidated = false
	private transportSub?: Subscription
	private scenarioDocDispose?: () => void
	sidebarAvailable = false
	private sidebarAvailabilityHandler = (ev: any) => {
		try {
			const available = !!ev?.detail?.available
			this.zone.run(() => {
				this.sidebarAvailable = available
			})
		} catch { }
	}
	private scenarioChangedHandler = (ev: any) => {
		try {
			const scenario = String(ev?.detail?.scenario || '').trim()
			if (!scenario) return
			this.zone.run(() => {
				this.currentScenario = scenario
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
		private channels: HubMemberChannelsService,
		private toastCtrl: ToastController,
	) {
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

		// Initialize semantic member-channel runtime and underlying transport visibility handling.
		this.channels.initRuntime()

		try {
			window.addEventListener('adaos:sidebarAvailability', this.sidebarAvailabilityHandler as any)
		} catch { }
		try {
			window.addEventListener('adaos:currentScenario', this.scenarioChangedHandler as any)
		} catch { }
		this.bindScenarioState()
		// If we loaded via cache-bust URL, clean it up for nicer sharing/bookmarks.
		setTimeout(() => this.stripVersionParam(), 0)

		// Keep this lightweight: it only drives a small "online/offline" indicator.
		// Polling too frequently creates log noise on the root proxy and on hubs.
		const hubProbe$ = timer(0, 15000).pipe(
			switchMap(() => {
				const { url, headers } = this.getHubStatusRequest()
				return this.http.get(url, { responseType: 'text', headers }).pipe(
					timeout(4500),
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
		this.hubStatus$ = combineLatest([
			hubProbe$,
			this.adaos.eventsConnectionState$,
			this.ydoc.syncConnectionState$,
		]).pipe(
			map(([probe, eventsState, syncState]) => {
				if (eventsState === 'connected' || syncState === 'connected') {
					return 'online' as const
				}
				if (probe === 'online') {
					return 'online' as const
				}
				if (
					probe === 'checking' ||
					eventsState === 'connecting' ||
					syncState === 'connecting'
				) {
					return 'checking' as const
				}
				return 'offline' as const
			}),
			distinctUntilChanged(),
		)

		// Header transport state should reflect the active semantic member path,
		// not just whether a raw WebRTC peer happens to be connected.
		this.transportState$ = this.channels.transportState$.pipe(
			distinctUntilChanged(),
		)

		// Toast notifications should also follow semantic member transport state,
		// not the raw RTC peer state.
		this.transportSub = this.transportState$.pipe(
			distinctUntilChanged(),
			pairwise(),
			filter(([prev, cur]) => {
				// Show toast for meaningful transitions
				return (prev === 'connected' && cur === 'ws') ||
					(prev === 'ws' && cur === 'connected') ||
					(prev === 'connecting' && cur === 'ws') ||
					(prev === 'signaling' && cur === 'ws') ||
					(prev === 'signaling' && cur === 'connected') ||
					(prev === 'connecting' && cur === 'connected') ||  // Recovery
					(prev === 'ws' && cur === 'connecting') ||
					(prev === 'ws' && cur === 'signaling')
			}),
		).subscribe(async ([prev, cur]) => {
			let message = ''
			let color: 'warning' | 'success' | 'primary' = 'success'

			if (cur === 'ws') {
				message = 'Direct connection unavailable. Using cloud relay — possible delays.'
				color = 'warning'
			} else if (cur === 'connected') {
				if (prev === 'ws' || prev === 'connecting' || prev === 'signaling') {
					message = 'Direct P2P connection established.'
					color = 'success'
				}
			} else if (cur === 'connecting' || cur === 'signaling') {
				if (prev === 'ws') {
					message = 'Reconnecting...'
					color = 'primary'
				}
			}

			if (message) {
				const toast = await this.toastCtrl.create({
					message,
					duration: 4000,
					position: 'bottom',
					color,
				})
				await toast.present()
			}
		})
	}

	private getHubStatusRequest(): { url: string; headers?: HttpHeaders } {
		// Prefer local hub if it is running (transparent local mode).
		// This also makes the browser show probe requests in Network, which helps debugging.
		try {
			const base = this.adaos.getBaseUrl().replace(/\/+$/, '')
			const host = new URL(base).hostname.toLowerCase()
			if (host === '127.0.0.1' || host === 'localhost' || host === '::1') {
				return { url: `${base}/api/ping` }
			}
		} catch {}
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

	private decodeJwtPayload(token: string): any | null {
		try {
			const parts = token.split('.')
			if (parts.length < 2) return null
			const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
			const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4)
			const json = atob(padded)
			return JSON.parse(json)
		} catch {
			return null
		}
	}

	get subnetId(): string | null {
		const jwt = this.readSessionJwt()
		if (jwt && jwt.includes('.')) {
			const payload = this.decodeJwtPayload(jwt)
			const raw = payload?.subnet_id || payload?.hub_id || payload?.owner_id
			if (typeof raw === 'string' && raw.trim()) {
				return raw.trim()
			}
		}
		try {
			const persisted = (localStorage.getItem('adaos_hub_id') || '').trim()
			return persisted || null
		} catch {
			return null
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
		this.transportSub?.unsubscribe()
		this.scenarioDocDispose?.()
		this.scenarioDocDispose = undefined
		this.colorSchemeMedia?.removeEventListener('change', this.colorSchemeListener)
		try {
			window.removeEventListener('adaos:sidebarAvailability', this.sidebarAvailabilityHandler as any)
		} catch { }
		try {
			window.removeEventListener('adaos:currentScenario', this.scenarioChangedHandler as any)
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

	private bindScenarioState(): void {
		this.scenarioDocDispose?.()
		this.syncCurrentScenarioFromDoc()
		try {
			const uiNode = this.ydoc.getPath('ui')
			this.scenarioDocDispose = observeDeep(uiNode, () => this.syncCurrentScenarioFromDoc())
		} catch {
			this.scenarioDocDispose = undefined
		}
	}

	private syncCurrentScenarioFromDoc(): void {
		let scenario = 'web_desktop'
		try {
			const raw = this.ydoc.toJSON(this.ydoc.getPath('ui/current_scenario'))
			if (typeof raw === 'string' && raw.trim()) {
				scenario = raw.trim()
			}
		} catch { }
		this.zone.run(() => {
			this.currentScenario = scenario
		})
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
		return Boolean(this.readSessionJwt() || this.isLocalHubTrusted())
	}

	get showCloseToDesktop(): boolean {
		const cur = String(this.currentScenario || '').trim()
		return !!cur && cur !== 'web_desktop'
	}

	private isLocalHubTrusted(): boolean {
		try {
			const base = this.adaos.getBaseUrl().replace(/\/+$/, '')
			const host = new URL(base).hostname.toLowerCase()
			return host === '127.0.0.1' || host === 'localhost' || host === '::1'
		} catch {
			return false
		}
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

	async onClickCloseToDesktop(): Promise<void> {
		return this.onClickHome()
	}

	async onClickYjsReload(): Promise<void> {
		if (!this.isAuthenticated) return
		const ws = this.ydoc.getWebspaceId() || 'default'
		// Keep the header refresh button semantically aligned with the existing
		// "YJS reload" action (reseed current webspace from scenario).
		await this.runWebspaceYjsAction('desktop.webspace.reload', ws)
	}

	private async runWebspaceYjsAction(
		command: 'desktop.webspace.reload' | 'desktop.webspace.reset',
		webspaceId: string,
	): Promise<void> {
		try {
			await this.adaos.sendEventsCommand(command, { webspace_id: webspaceId })
		} catch (err) {
			// eslint-disable-next-line no-console
			console.warn(`${command} failed`, err)
			return
		}
		try {
			await this.ydoc.clearStorage()
		} catch { }
		try {
			location.reload()
		} catch { }
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
