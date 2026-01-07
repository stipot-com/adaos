// src\adaos\integrations\inimatic\src\app\renderer\desktop\desktop.component.ts
import { Component, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule, ModalController } from '@ionic/angular'
import { YDocService } from '../../y/ydoc.service'
import { AdaosClient } from '../../core/adaos/adaos-client.service'
import { observeDeep } from '../../y/y-helpers'
import { AdaApp } from '../../runtime/dsl-types'
import { PageSchema, WidgetConfig } from '../../runtime/page-schema.model'
import { DesktopSchemaService } from '../../runtime/desktop-schema.service'
import { ModalHostComponent } from '../modals/modal.component'
import { PageWidgetHostComponent } from '../widgets/page-widget-host.component'
import { LoginComponent } from '../../features/login/login.component'
import { PageStateService, PageState } from '../../runtime/page-state.service'
import { Subscription } from 'rxjs'
import { QRCodeModule } from 'angularx-qrcode'
import { PairingService } from '../../runtime/pairing.service'
import { TPipe } from '../../runtime/t.pipe'
import { addIcons } from 'ionicons'
import { menuOutline, closeOutline, homeOutline, ellipsisHorizontalOutline } from 'ionicons/icons'

@Component({
	selector: 'ada-desktop',
	standalone: true,
	imports: [CommonModule, IonicModule, PageWidgetHostComponent, LoginComponent, QRCodeModule, TPipe],
	templateUrl: './desktop.component.html',
	styleUrls: ['./desktop.component.scss']
})
export class DesktopRendererComponent implements OnInit, OnDestroy {
	app?: AdaApp
	dispose?: () => void
	private compactMedia?: MediaQueryList
	private mediaHandlersBound = false
	private mediaApplyHandler?: () => void
	isCompact = false
	sidebarOpen = false
	private collapsedWidgetIds = new Set<string>()
	fabActions: Array<{ id: string; icon?: string; label?: string; cmd?: string; payload?: any }> = []
	webspaces: Array<{ id: string; title: string; created_at: number }> = []
	activeWebspace = 'default'
	pageSchema?: PageSchema
	private areaWidgetCounts = new Map<string, number>()
	private lastSidebarAvailable?: boolean
	private stateSub?: Subscription
	isAuthenticated = false
	needsLogin = false
	initError = ''
	needsPairing = false
	pairingId = ''
	pairingUrl = ''
	pairStatusKey = ''
	pairStatusParams: Record<string, any> | undefined
	pairCode = ''
	pendingApproveCode = ''
	selectedApproveWebspace = ''
	private pairPollTimer?: any
	private pairRecreateInFlight = false
	constructor(
		private y: YDocService,
		private modal: ModalController,
		private adaos: AdaosClient,
		private desktopSchema: DesktopSchemaService,
		private pageState: PageStateService,
		private pairing: PairingService,
	) { }

	async ngOnInit() {
		addIcons({ menuOutline, closeOutline, homeOutline, ellipsisHorizontalOutline })
		this.ensureMediaQueries()
		try {
			window.addEventListener('adaos:toggleSidebar', this.onToggleSidebar as any)
		} catch {}
		this.pendingApproveCode = this.readPairCodeFromUrl()
		this.isAuthenticated = this.hasOwnerSession()
		if (!this.isAuthenticated) {
			this.needsPairing = !this.pendingApproveCode
			if (this.needsPairing) this.ensurePairing()
			return
		}
		try {
			await this.y.initFromHub()
		} catch (e) {
			const msg = String((e as any)?.message || e || '')
			this.initError = msg
			if (
				msg.includes('hub_unreachable_no_session') ||
				msg.includes('session_invalid')
			) {
				this.needsLogin = true
				return
			}
			// Do not crash the renderer; show a retry UI instead.
			return
		}
		const uiNode = this.y.getPath('ui')
		const dataNode = this.y.getPath('data')
		const recompute = () => {
			this.app = this.y.toJSON(this.y.getPath('ui/application'))
			this.readFabActions()
			this.readWebspaces()
			this.pageSchema = this.desktopSchema.loadSchema()
			this.rebuildAreaWidgetCounts()
			this.emitSidebarAvailability()
			if (this.isCompact) this.initCollapsedWidgets()
			this.selectedApproveWebspace =
				this.selectedApproveWebspace || this.activeWebspace || 'default'
		}
		recompute()
		const un1 = observeDeep(uiNode, recompute)
		const un2 = observeDeep(dataNode, recompute)
		this.stateSub?.unsubscribe()
		this.stateSub = this.pageState.selectAll().subscribe(() => {
			this.rebuildAreaWidgetCounts()
		})
		this.dispose = () => {
			un1?.()
			un2?.()
			this.stateSub?.unsubscribe()
			try { clearInterval(this.pairPollTimer) } catch {}
			this.teardownMediaQueries()
			try {
				window.removeEventListener('adaos:toggleSidebar', this.onToggleSidebar as any)
			} catch {}
			try {
				this.lastSidebarAvailable = undefined
				window.dispatchEvent(new CustomEvent('adaos:sidebarAvailability', { detail: { available: false } }))
			} catch {}
		}
	}
	ngOnDestroy() {
		try { this.teardownMediaQueries() } catch {}
		this.dispose?.()
	}

	private ensureMediaQueries(): void {
		if (this.mediaHandlersBound) return
		try {
			this.compactMedia = window.matchMedia('(max-width: 1100px)')
			const apply = () => {
				const prevCompact = this.isCompact
				this.isCompact = !!this.compactMedia?.matches
				// Sidebar drawer is controlled purely by CSS breakpoints; still close it when switching layouts.
				try {
					const narrow = window.matchMedia('(max-width: 900px)').matches
					if (!narrow) this.sidebarOpen = false
				} catch {}
				if (!prevCompact && this.isCompact) {
					this.initCollapsedWidgets()
				}
			}
			this.mediaApplyHandler = apply
			apply()
			// Safari fallback: MediaQueryList may only support addListener/removeListener.
			const anyCompact: any = this.compactMedia as any
			if (typeof anyCompact?.addEventListener === 'function') {
				this.compactMedia.addEventListener('change', apply)
			} else if (typeof anyCompact?.addListener === 'function') {
				anyCompact.addListener(apply)
			}
			this.mediaHandlersBound = true
		} catch {}
	}

	private onToggleSidebar = () => {
		this.toggleSidebar()
	}

	private teardownMediaQueries(): void {
		if (!this.mediaHandlersBound) return
		try {
			if (this.mediaApplyHandler) {
				const anyCompact: any = this.compactMedia as any
				if (typeof anyCompact?.removeEventListener === 'function') {
					this.compactMedia?.removeEventListener('change', this.mediaApplyHandler)
				} else if (typeof anyCompact?.removeListener === 'function') {
					anyCompact.removeListener(this.mediaApplyHandler)
				}
			}
		} catch {}
		this.mediaApplyHandler = undefined
		this.mediaHandlersBound = false
	}

	toggleSidebar(): void {
		this.sidebarOpen = !this.sidebarOpen
	}

	private emitSidebarAvailability(): void {
		const available = this.roleHasWidgets('aux')
		if (this.lastSidebarAvailable === available) return
		this.lastSidebarAvailable = available
		try {
			window.dispatchEvent(new CustomEvent('adaos:sidebarAvailability', { detail: { available } }))
		} catch {}
	}

	closeSidebar(): void {
		this.sidebarOpen = false
	}

	private readFabActions(): void {
		const raw = (this.app as any)?.desktop?.fab?.actions
		this.fabActions = Array.isArray(raw) ? raw : []
	}

	async fabHome(): Promise<void> {
		try {
			const ws = this.y.getWebspaceId()
			await this.adaos.sendEventsCommand('desktop.scenario.set', {
				scenario_id: 'web_desktop',
				webspace_id: ws || undefined,
			})
		} catch {}
	}

	async onFabAction(a: { id: string; cmd?: string; payload?: any }): Promise<void> {
		if (!a) return
		if (a.id === 'menu') {
			this.toggleSidebar()
			return
		}
		if (a.id === 'home') {
			await this.fabHome()
			return
		}
		if (a.cmd) {
			try {
				await this.adaos.sendEventsCommand(String(a.cmd), a.payload || {})
			} catch {}
		}
	}

	roleHasWidgets(role: string): boolean {
		const page = this.pageSchema
		if (!page?.layout?.areas?.length) return false
		for (const area of page.layout.areas) {
			if (area.role === role && this.areaHasWidgets(area.id)) return true
		}
		return false
	}

	hasRole(role: string): boolean {
		const page = this.pageSchema
		if (!page?.layout?.areas?.length) return false
		return page.layout.areas.some((a) => a.role === role)
	}

	widgetIsCollapsible(widget: WidgetConfig): boolean {
		const flag = (widget.inputs as any)?.collapsible
		return !!flag
	}

	widgetIsCollapsed(widget: WidgetConfig): boolean {
		return this.collapsedWidgetIds.has(widget.id)
	}

	toggleWidgetCollapsed(widget: WidgetConfig): void {
		if (this.collapsedWidgetIds.has(widget.id)) {
			this.collapsedWidgetIds.delete(widget.id)
			return
		}
		this.collapsedWidgetIds.add(widget.id)
	}

	private initCollapsedWidgets(): void {
		const page = this.pageSchema
		if (!page?.layout?.areas?.length || !Array.isArray(page.widgets)) return
		const auxAreaIds = page.layout.areas
			.filter((a) => a.role === 'aux')
			.map((a) => a.id)
		for (const w of page.widgets) {
			if (!auxAreaIds.includes(w.area)) continue
			if (!this.widgetIsCollapsible(w)) continue
			if (this.collapsedWidgetIds.has(w.id)) continue
			const defaultCollapsed = (w.inputs as any)?.collapsedByDefault
			const shouldCollapse = defaultCollapsed === undefined ? true : !!defaultCollapsed
			if (shouldCollapse) this.collapsedWidgetIds.add(w.id)
		}
	}

	async onLoginSuccess() {
		// After login, sessionJwt + hubId are persisted to localStorage by LoginService.
		// Re-run init; it will probe local hub and fall back to root proxy if needed.
		this.needsLogin = false
		this.needsPairing = false
		this.isAuthenticated = true
		this.initError = ''
		try {
			localStorage.removeItem('adaos_pair_code')
		} catch {}
		try { clearInterval(this.pairPollTimer) } catch {}
		await this.ngOnInit()
	}

	async retryInit(): Promise<void> {
		this.initError = ''
		try {
			await this.ngOnInit()
		} catch {}
	}

	logout(): void {
		const keys = [
			'adaos_web_session_jwt',
			'adaos_hub_id',
			'adaos_web_sid',
			'adaos_hub_base',
			'adaos_webspace_id',
		]
		for (const key of keys) {
			try {
				localStorage.removeItem(key)
			} catch {}
		}
		try {
			location.reload()
		} catch {}
	}

	private hasOwnerSession(): boolean {
		try {
			const jwt = localStorage.getItem('adaos_web_session_jwt')
			return !!(jwt && jwt.trim())
		} catch {
			return false
		}
	}

	private ensurePairing(): void {
		this.pairStatusKey = 'pair.status.creating'
		this.pairStatusParams = undefined
		const cached = (() => {
			try {
				return (localStorage.getItem('adaos_pair_code') || '').trim()
			} catch {
				return ''
			}
		})()
		if (cached) {
			this.pairCode = cached
			this.buildPairingUrl()
			this.startPairingPoll()
			return
		}
		this.pairing.createBrowserPair(600).subscribe({
			next: (res) => {
				if (!res?.ok || !res.pair_code) {
					this.pairStatusKey = 'pair.status.create_failed'
					return
				}
				this.pairRecreateInFlight = false
				this.pairCode = res.pair_code
				try {
					localStorage.setItem('adaos_pair_code', this.pairCode)
				} catch {}
				this.buildPairingUrl()
				this.startPairingPoll()
			},
			error: () => {
				this.pairStatusKey = 'pair.status.create_failed'
			},
		})
	}

	private buildPairingUrl(): void {
		const origin = (() => {
			try {
				return window.location.origin
			} catch {
				return ''
			}
		})()
		this.pairingId = this.pairCode
		this.pairingUrl = `${origin}/?pair_code=${encodeURIComponent(this.pairCode)}`
	}

	private startPairingPoll(): void {
		try { clearInterval(this.pairPollTimer) } catch {}
		this.pairStatusKey = 'pair.status.waiting'
		this.pairStatusParams = undefined
		this.pairPollTimer = setInterval(() => {
			this.pairing.getBrowserPairStatus(this.pairCode).subscribe({
				next: (res) => {
					if (!res?.ok) return
					if (res.state === 'approved' && res.session_jwt && res.hub_id) {
						this.pairStatusKey = 'pair.status.connecting'
						this.pairStatusParams = undefined
						try { localStorage.setItem('adaos_web_session_jwt', res.session_jwt) } catch {}
						try { localStorage.setItem('adaos_hub_id', res.hub_id) } catch {}
						if (res.webspace_id) {
							try { localStorage.setItem('adaos_webspace_id', res.webspace_id) } catch {}
						}
						try { localStorage.removeItem('adaos_pair_code') } catch {}
						try { clearInterval(this.pairPollTimer) } catch {}
						try { location.reload() } catch {}
					}
					if (
						res.state === 'not_found' ||
						res.state === 'expired' ||
						res.state === 'revoked'
					) {
						if (this.pairRecreateInFlight) {
							this.pairStatusKey = 'pair.status.regenerating'
							this.pairStatusParams = { state: String(res.state) }
							return
						}
						this.pairRecreateInFlight = true
						this.pairStatusKey = 'pair.status.regenerating'
						this.pairStatusParams = { state: String(res.state) }
						try { clearInterval(this.pairPollTimer) } catch {}
						try { localStorage.removeItem('adaos_pair_code') } catch {}
						this.pairCode = ''
						this.pairingUrl = ''
						// Re-create on next tick to avoid reentrancy.
						setTimeout(() => this.ensurePairing(), 50)
					}
				},
				error: () => {},
			})
		}, 2000)
	}

	private readPairCodeFromUrl(): string {
		try {
			const url = new URL(window.location.href)
			return (
				(url.searchParams.get('pair_code') || url.searchParams.get('pair') || '').trim()
			)
		} catch {
			return ''
		}
	}

	approvePairing(): void {
		const code = (this.pendingApproveCode || '').trim()
		if (!code) return
		const ws = (this.selectedApproveWebspace || this.activeWebspace || 'default').trim() || 'default'
		this.pairStatusKey = 'pair.approve.status.approving'
		this.pairStatusParams = undefined
		this.pairing.approveBrowserPair(code, ws).subscribe({
			next: (res) => {
				if (res?.ok) {
					this.pairStatusKey = 'pair.approve.status.approved'
					this.pairStatusParams = undefined
				} else {
					this.pairStatusKey = 'pair.approve.status.failed'
					this.pairStatusParams = { error: String(res?.error || 'unknown_error') }
				}
				if (res?.ok) {
					try {
						const u = new URL(window.location.href)
						u.searchParams.delete('pair_code')
						u.searchParams.delete('pair')
						window.history.replaceState({}, '', u.toString())
					} catch {}
					this.pendingApproveCode = ''
				}
			},
			error: (err: any) => {
				const status = err?.status ? `HTTP ${err.status}` : 'HTTP error'
				const code = err?.error?.error || err?.error?.code || err?.message || 'unknown'
				this.pairStatusKey = 'pair.approve.status.failed'
				this.pairStatusParams = { error: `${status} ${code}`.trim() }
			},
		})
	}

	async openModal(id: string) {
		const modalCfg: any = (this.app as any)?.modals?.[id]
		if (!modalCfg) return
		const type = modalCfg.type
		const source = String(modalCfg.source || '')
		const data = source ? this.y.toJSON(this.y.getPath(source.replace('y:', ''))) : undefined
		// prepare common cfg
		const cfg: any = { title: modalCfg.title, data }

		// catalogs need callbacks
		if (type === 'catalog-apps' || type === 'catalog-widgets') {
			const doc = this.y.doc
			const installedPath = type === 'catalog-apps' ? 'data/installed/apps' : 'data/installed/widgets'
			const itemsPath = type === 'catalog-apps' ? 'data/catalog/apps' : 'data/catalog/widgets'
			const items = this.y.toJSON(this.y.getPath(itemsPath)) || []

			const isInstalled = (it: any) => {
				const cur: string[] = (this.y.toJSON(this.y.getPath(installedPath)) || []) as string[]
				return cur.includes(it.id)
			}
			const toggle = (it: any) => {
				const kind = type === 'catalog-apps' ? 'app' : 'widget'
				this.syncToggleInstall(kind, it.id)
			}
			const modalRef = await this.modal.create({
				component: ModalHostComponent,
				componentProps: { type, cfg: { title: modalCfg.title, items, isInstalled, toggle, close: () => modalRef.dismiss() } }
			})
			await modalRef.present()
			return
		}

		const m = await this.modal.create({ component: ModalHostComponent, componentProps: { type, cfg } })
		await m.present()
	}

	get webspaceLabel(): string {
		const entry = this.webspaces.find(ws => ws.id === this.activeWebspace)
		return entry?.title || this.activeWebspace
	}

	private readWebspaces() {
		const raw = this.y.toJSON(this.y.getPath('data/webspaces'))
		const items = Array.isArray(raw?.items) ? raw.items : []
		this.webspaces = items
		this.activeWebspace = this.y.getWebspaceId()
	}

	private async syncToggleInstall(type: 'app' | 'widget', id: string) {
		try {
			await this.adaos.sendEventsCommand('desktop.toggleInstall', { type, id })
		} catch (err) {
			console.warn('desktop.toggleInstall failed', err)
		}
	}

	async onWebspaceChanged(ev: CustomEvent) {
		const target = ev.detail?.value
		if (!target || target === this.activeWebspace) return
		try {
			await this.y.switchWebspace(target)
		} catch (err) {
			console.warn('webspace switch failed', err)
		}
	}

	async createWebspace() {
		const suggested = `space-${Date.now().toString(16)}`
		const rawId = prompt('ID нового webspace', suggested)
		if (!rawId) return
		const title = prompt('Название webspace', rawId) ?? rawId
		try {
			await this.adaos.sendEventsCommand('desktop.webspace.create', { id: rawId, title })
			await this.y.switchWebspace(rawId)
			this.activeWebspace = rawId
		} catch (err) {
			console.warn('webspace create failed', err)
		}
	}

	async renameWebspace() {
		if (!this.activeWebspace) return
		const entry = this.webspaces.find(ws => ws.id === this.activeWebspace)
		const nextTitle = prompt('Новое имя webspace', entry?.title || this.activeWebspace)
		if (!nextTitle) return
		try {
			await this.adaos.sendEventsCommand('desktop.webspace.rename', { id: this.activeWebspace, title: nextTitle })
		} catch (err) {
			console.warn('webspace rename failed', err)
		}
	}

	async deleteWebspace() {
		if (!this.activeWebspace || this.activeWebspace === 'default' || this.activeWebspace === 'desktop') return
		const entry = this.webspaces.find(ws => ws.id === this.activeWebspace)
		const ok = confirm(`Удалить webspace "${entry?.title || this.activeWebspace}"?`)
		if (!ok) return
		try {
			await this.adaos.sendEventsCommand('desktop.webspace.delete', { id: this.activeWebspace })
			await this.y.switchWebspace('default')
			this.activeWebspace = 'default'
		} catch (err) {
			console.warn('webspace delete failed', err)
		}
	}

	async refreshWebspaces() {
		try {
			await this.adaos.sendEventsCommand('desktop.webspace.refresh', {})
		} catch (err) {
			console.warn('webspace refresh failed', err)
		}
	}

	async resetDb() {
		await this.y.clearStorage()
		location.reload()
	}

	getWidgetById(id: string): WidgetConfig | undefined {
		return this.pageSchema?.widgets.find(w => w.id === id)
	}

	getWidgetsInArea(areaId: string): WidgetConfig[] {
		return (this.pageSchema?.widgets || []).filter(w => w.area === areaId)
	}

	areaHasWidgets(areaId: string): boolean {
		return (this.areaWidgetCounts.get(areaId) || 0) > 0
	}

	desktopLayoutClass(page: PageSchema): Record<string, boolean> {
		const roles = new Map<string, boolean>()
		for (const area of page.layout.areas || []) {
			if (!area?.role) continue
			if (this.areaHasWidgets(area.id)) roles.set(area.role, true)
		}
		const hasSidebar = roles.get('sidebar') === true
		const hasAux = roles.get('aux') === true
		return {
			'no-sidebar': !hasSidebar,
			'no-aux': !hasAux,
		}
	}

	private rebuildAreaWidgetCounts() {
		const state = this.pageState.getSnapshot()
		const map = new Map<string, number>()
		for (const w of this.pageSchema?.widgets || []) {
			if (!w.area) continue
			if (!this.evaluateVisibility(w, state)) continue
			map.set(w.area, (map.get(w.area) || 0) + 1)
		}
		this.areaWidgetCounts = map
	}

	private evaluateVisibility(widget: WidgetConfig, state: PageState): boolean {
		const expr = (widget?.visibleIf || '').trim()
		if (!expr) return true
		if (expr.startsWith('$state.')) {
			const parts = expr.split('===')
			if (parts.length === 2) {
				const key = parts[0].trim().slice('$state.'.length)
				const rawValue = parts[1].trim()
				const expected = this.parseLiteral(rawValue)
				return state[key] === expected
			}
		}
		return true
	}

	private parseLiteral(raw: string): any {
		if (raw === 'true') return true
		if (raw === 'false') return false
		const quoted = raw.match(/^['"](.+)['"]$/)
		if (quoted) return quoted[1]
		return raw
	}
}
