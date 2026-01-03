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

@Component({
	selector: 'ada-desktop',
	standalone: true,
	imports: [CommonModule, IonicModule, PageWidgetHostComponent, LoginComponent, QRCodeModule],
	templateUrl: './desktop.component.html',
	styleUrls: ['./desktop.component.scss']
})
export class DesktopRendererComponent implements OnInit, OnDestroy {
	app?: AdaApp
	dispose?: () => void
	webspaces: Array<{ id: string; title: string; created_at: number }> = []
	activeWebspace = 'default'
	pageSchema?: PageSchema
	private areaWidgetCounts = new Map<string, number>()
	private stateSub?: Subscription
	isAuthenticated = false
	needsLogin = false
	initError = ''
	needsPairing = false
	pairingId = ''
	pairingUrl = ''
	pairStatusText = ''
	pairCode = ''
	pendingApproveCode = ''
	selectedApproveWebspace = ''
	private pairPollTimer?: any
	private pairRecreateInFlight = false
	pairApiBase = ''
	constructor(
		private y: YDocService,
		private modal: ModalController,
		private adaos: AdaosClient,
		private desktopSchema: DesktopSchemaService,
		private pageState: PageStateService,
		private pairing: PairingService,
	) { }

	async ngOnInit() {
		this.applyPairBaseFromUrl()
		this.pairApiBase = this.pairing.getBaseUrl()
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
			throw e
		}
		const uiNode = this.y.getPath('ui')
		const dataNode = this.y.getPath('data')
		const recompute = () => {
			this.app = this.y.toJSON(this.y.getPath('ui/application'))
			this.readWebspaces()
			this.pageSchema = this.desktopSchema.loadSchema()
			this.rebuildAreaWidgetCounts()
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
		}
	}
	ngOnDestroy() { this.dispose?.() }

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

	private hasOwnerSession(): boolean {
		try {
			const jwt = localStorage.getItem('adaos_web_session_jwt')
			return !!(jwt && jwt.trim())
		} catch {
			return false
		}
	}

	private ensurePairing(): void {
		this.pairStatusText = 'creating pairing...'
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
					this.pairStatusText = 'failed to create pairing'
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
				this.pairStatusText = 'failed to create pairing'
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
		const base = (() => {
			try {
				return this.pairing.getBaseUrl()
			} catch {
				return ''
			}
		})()
		const baseParam = base ? `&pair_base=${encodeURIComponent(base)}` : ''
		this.pairingUrl = `${origin}/desktop2?pair_code=${encodeURIComponent(this.pairCode)}${baseParam}`
	}

	private startPairingPoll(): void {
		try { clearInterval(this.pairPollTimer) } catch {}
		this.pairStatusText = 'waiting for approval...'
		this.pairPollTimer = setInterval(() => {
			this.pairing.getBrowserPairStatus(this.pairCode).subscribe({
				next: (res) => {
					if (!res?.ok) return
					if (res.state === 'approved' && res.session_jwt && res.hub_id) {
						this.pairStatusText = 'approved, connecting...'
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
							this.pairStatusText = `pairing ${String(res.state)}`
							return
						}
						this.pairRecreateInFlight = true
						this.pairStatusText = `pairing ${String(res.state)}, regenerating...`
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

	private applyPairBaseFromUrl(): void {
		try {
			const url = new URL(window.location.href)
			const base = (url.searchParams.get('pair_base') || '').trim()
			if (base) this.pairing.setBaseUrl(base)
		} catch {
			// ignore
		}
	}

	approvePairing(): void {
		const code = (this.pendingApproveCode || '').trim()
		if (!code) return
		const ws = (this.selectedApproveWebspace || this.activeWebspace || 'default').trim() || 'default'
		this.pairStatusText = 'approving...'
		this.pairing.approveBrowserPair(code, ws).subscribe({
			next: (res) => {
				this.pairStatusText = res?.ok ? 'approved' : `approve failed: ${res?.error || 'unknown_error'}`
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
				this.pairStatusText = `approve failed: ${status} ${code}`
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
