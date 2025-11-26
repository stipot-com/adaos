// src\adaos\integrations\inimatic\src\app\renderer\desktop\desktop.component.ts
import { Component, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule, ModalController } from '@ionic/angular'
import { YDocService } from '../../y/ydoc.service'
import { AdaosClient } from '../../core/adaos/adaos-client.service'
import { observeDeep } from '../../y/y-helpers'
import { AdaApp } from '../../runtime/dsl-types'
import { PageSchema, WidgetConfig, LayoutConfig, ActionConfig } from '../../runtime/page-schema.model'
import { WeatherWidgetComponent } from '../widgets/weather-widget.component'
import { WidgetComponent } from '../widgets/widget.component'
import { ModalHostComponent } from '../modals/modal.component'
import '../../runtime/registry.weather'
import '../../runtime/registry.catalogs'

@Component({
	selector: 'ada-desktop',
	standalone: true,
	imports: [CommonModule, IonicModule, WeatherWidgetComponent, WidgetComponent],
	templateUrl: './desktop.component.html',
	styleUrls: ['./desktop.component.scss']
})
export class DesktopRendererComponent implements OnInit, OnDestroy {
	app?: AdaApp
	dispose?: () => void
	resolvedIcons: Array<{ id: string; title: string; icon: string; action?: any; dev?: boolean }> = []
	resolvedWidgets: Array<{ id: string; type: string; title?: string; source?: string; dev?: boolean }> = []
	webspaces: Array<{ id: string; title: string; created_at: number }> = []
	activeWebspace = 'default'
	pageSchema?: PageSchema
	constructor(private y: YDocService, private modal: ModalController, private adaos: AdaosClient) { }

	async ngOnInit() {
		await this.y.initFromHub()
		const appNode = this.y.getPath('ui/application')
		const dataNode = this.y.getPath('data')
		const recompute = () => {
			this.app = this.y.toJSON(appNode)
			this.rebuildFromInstalled()
			this.readWebspaces()
			this.buildPageSchema()
		}
		recompute()
		const un1 = observeDeep(appNode, recompute)
		const un2 = observeDeep(dataNode, recompute)
		this.dispose = () => { un1?.(); un2?.() }
	}
	ngOnDestroy() { this.dispose?.() }

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
	getData(source?: string) {
		if (!source) return undefined
		const path = source.startsWith('y:') ? source.slice(2) : source
		return this.y.toJSON(this.y.getPath(path))
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

	private rebuildFromInstalled() {
		// resolve icons from data.desktop.installed.apps (fallback data.installed.apps) + data.catalog.apps
		const catalogApps: any[] = this.y.toJSON(this.y.getPath('data/catalog/apps')) || []
		const installedApps: string[] = this.y.toJSON(this.y.getPath('data/installed/apps')) || []
		const byId: Record<string, any> = {}
		for (const it of catalogApps) byId[it.id] = it
		this.resolvedIcons = installedApps
			.map(id => byId[id])
			.filter(Boolean)
			.map(it => ({
				id: it.id,
				title: it.title || it.id,
				icon: it.icon || (this.app as any)?.desktop?.iconTemplate?.icon || 'apps-outline',
				action: it.launchModal ? { openModal: it.launchModal } : undefined,
				dev: !!it.dev,
			}))

		// resolve widgets from data.desktop.installed.widgets (fallback data.installed.widgets) + data.catalog.widgets
		const catalogWidgets: any[] = this.y.toJSON(this.y.getPath('data/catalog/widgets')) || []
		const installedWidgets: string[] = this.y.toJSON(this.y.getPath('data/installed/widgets')) || []
		const wById: Record<string, any> = {}
		for (const it of catalogWidgets) wById[it.id] = it
		this.resolvedWidgets = installedWidgets
			.map(id => wById[id])
			.filter(Boolean)
			.map(it => ({ id: it.id, type: it.type, title: it.title, source: it.source, dev: !!it.dev }))
	}

	private buildPageSchema() {
		const desktop: any = this.app?.desktop
		const baseSchema: PageSchema | undefined = desktop?.pageSchema

		if (!baseSchema) {
			this.buildLegacyPageSchema()
			return
		}

		// Deep copy so we never mutate YDoc-backed state directly.
		const schema: PageSchema = JSON.parse(JSON.stringify(baseSchema))
		this.enrichPageSchemaFromY(schema)
		this.pageSchema = schema
	}

	private buildLegacyPageSchema() {
		const desktop = this.app?.desktop
		const layout: LayoutConfig = {
			type: 'single',
			areas: [
				{ id: 'topbar', role: 'header', label: 'Topbar' },
				{ id: 'icons', role: 'main', label: 'Icons' },
				{ id: 'widgets', role: 'main', label: 'Widgets' },
			],
		}

		const widgets: WidgetConfig[] = []

		if (desktop?.topbar?.length) {
			widgets.push({
				id: 'topbar',
				area: 'topbar',
				type: 'desktop.widgets',
				title: 'Topbar',
				inputs: {
					buttons: desktop.topbar,
				},
			})
		}

		if (this.resolvedIcons.length) {
			widgets.push({
				id: 'desktop-icons',
				area: 'icons',
				type: 'desktop.widgets',
				title: 'Applications',
				inputs: {
					items: this.resolvedIcons,
				},
			})
		}

		if (this.resolvedWidgets.length) {
			widgets.push({
				id: 'desktop-widgets',
				area: 'widgets',
				type: 'desktop.widgets',
				title: 'Widgets',
				inputs: {
					items: this.resolvedWidgets,
				},
			})
		}

		this.pageSchema = {
			id: 'desktop',
			title: 'Desktop',
			layout,
			widgets,
		}
	}

	private enrichPageSchemaFromY(schema: PageSchema) {
		if (!schema?.widgets?.length) return

		for (const w of schema.widgets) {
			const ds: any = w.dataSource as any
			if (!ds || ds.kind !== 'y') continue

			if (ds.path) {
				const data = this.y.toJSON(this.y.getPath(ds.path))
				if (w.id === 'topbar') {
					w.inputs = w.inputs || {}
					w.inputs['buttons'] = Array.isArray(data) ? data : []
				} else {
					w.inputs = w.inputs || {}
					w.inputs['data'] = data
				}
			}

			if (ds.transform === 'desktop.icons') {
				w.inputs = w.inputs || {}
				w.inputs['items'] = this.resolvedIcons
			}
			if (ds.transform === 'desktop.widgets') {
				w.inputs = w.inputs || {}
				w.inputs['items'] = this.resolvedWidgets
			}
		}

		// Fallback: if topbar buttons were not populated from YDoc for some reason,
		// ensure they are taken from app.desktop.topbar so Apps/Widgets remain visible.
		const desktop: any = this.app?.desktop
		if (desktop?.topbar?.length) {
			const topbarWidget = schema.widgets.find(w => w.id === 'topbar')
			if (topbarWidget) {
				topbarWidget.inputs = topbarWidget.inputs || {}
				const existing = topbarWidget.inputs['buttons']
				if (!Array.isArray(existing) || !existing.length) {
					topbarWidget.inputs['buttons'] = desktop.topbar
				}
			}
		}
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

	onWidgetEvent(widgetId: string, eventType: string, payload: any) {
		const widget = this.getWidgetById(widgetId)
		if (!widget?.actions?.length) return

		for (const act of widget.actions) {
			if (!this.matchesEvent(act.on, eventType)) continue
			this.executeAction(act, payload)
		}
	}

	private matchesEvent(on: string, eventType: string): boolean {
		return on === eventType
	}

	private executeAction(action: ActionConfig, payload: any) {
		switch (action.type) {
			case 'openModal':
				this.handleOpenModal(action, payload)
				break
			default:
				break
		}
	}

	private handleOpenModal(action: ActionConfig, payload: any) {
		const raw = action.params?.['modalId']
		if (!raw) return

		let modalId: string | undefined

		if (typeof raw === 'string' && raw.startsWith('$event.')) {
			modalId = this.resolveFromEvent(raw, payload)
		} else if (typeof raw === 'string') {
			modalId = raw
		}

		if (!modalId) return
		this.openModal(modalId)
	}

	private resolveFromEvent(expr: string, payload: any): string | undefined {
		if (!expr.startsWith('$event.')) return undefined
		const path = expr.slice('$event.'.length).split('.')

		let cur: any = payload
		for (const key of path) {
			if (cur == null) return undefined
			cur = cur[key]
		}

		return typeof cur === 'string' ? cur : undefined
	}

	getWidgetById(id: string): WidgetConfig | undefined {
		return this.pageSchema?.widgets.find(w => w.id === id)
	}

	getWidgetsInArea(areaId: string): WidgetConfig[] {
		return (this.pageSchema?.widgets || []).filter(w => w.area === areaId)
	}
}
