import { Component, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule, ModalController } from '@ionic/angular'
import { YDocService } from '../../y/ydoc.service'
import { AdaosClient } from '../../core/adaos/adaos-client.service'
import { observeDeep } from '../../y/y-helpers'
import { AdaApp } from '../../runtime/dsl-types'
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
  resolvedIcons: Array<{ id:string; title:string; icon:string; action?: any; dev?: boolean }> = []
  resolvedWidgets: Array<{ id:string; type:string; title?:string; source?:string; dev?: boolean }> = []
  webspaces: Array<{ id: string; title: string; created_at: number }> = []
  activeWebspace = 'default'
  constructor(private y: YDocService, private modal: ModalController, private adaos: AdaosClient) {}

  async ngOnInit() {
    await this.y.initFromHub()
    const appNode = this.y.getPath('ui/application')
    const dataNode = this.y.getPath('data')
    const recompute = () => {
      this.app = this.y.toJSON(appNode)
      this.rebuildFromInstalled()
      this.readWebspaces()
    }
    recompute()
    const un1 = observeDeep(appNode, recompute)
    const un2 = observeDeep(dataNode, recompute)
    this.dispose = () => { un1?.(); un2?.() }
  }
  ngOnDestroy(){ this.dispose?.() }

  async openModal(id: string) {
    const modalCfg: any = (this.app as any)?.modals?.[id]
    if (!modalCfg) return
    const type = modalCfg.type
    const source = String(modalCfg.source || '')
    const data = source ? this.y.toJSON(this.y.getPath(source.replace('y:',''))) : undefined
    // prepare common cfg
    const cfg: any = { title: modalCfg.title, data }

    // catalogs need callbacks
    if (type === 'catalog-apps' || type === 'catalog-widgets') {
      const doc = this.y.doc
      const installedPath = type === 'catalog-apps' ? 'data/installed/apps' : 'data/installed/widgets'
      const itemsPath = type === 'catalog-apps' ? 'data/catalog/apps' : 'data/catalog/widgets'
      const items = this.y.toJSON(this.y.getPath(itemsPath)) || []
      let installed: string[] = (this.y.toJSON(this.y.getPath(installedPath)) || []) as string[]

      const isInstalled = (it: any) => installed.includes(it.id)
      const toggle = (it: any) => {
        const set = new Set(installed)
        if (set.has(it.id)) {
          set.delete(it.id)
        } else {
          set.add(it.id)
        }
        const next = Array.from(set)
        doc.transact(() => {
          const dataMap: any = this.y.doc.getMap('data')
          const installedCur = this.y.toJSON(dataMap.get('installed')) || {}
          const nextInstalled = {
            apps: (type === 'catalog-apps') ? next : (installedCur.apps || installed),
            widgets: (type === 'catalog-widgets') ? next : (installedCur.widgets || installed)
          }
          dataMap.set('installed', nextInstalled)
        })
        installed = next
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
  onTopbarAction(btn: any){
    const act = btn?.action
    if (act?.openModal) this.openModal(act.openModal)
  }
  onIconClick(icon: any) {
    const act = icon?.action
    if (act?.openModal) this.openModal(act.openModal)
  }
  getData(source?: string){
    if (!source) return undefined
    const path = source.startsWith('y:') ? source.slice(2) : source
    return this.y.toJSON(this.y.getPath(path))
  }

  get webspaceLabel(): string {
    const entry = this.webspaces.find(ws => ws.id === this.activeWebspace)
    return entry?.title || this.activeWebspace
  }

  private readWebspaces(){
    const raw = this.y.toJSON(this.y.getPath('data/webspaces'))
    const items = Array.isArray(raw?.items) ? raw.items : []
    this.webspaces = items
    this.activeWebspace = this.y.getWebspaceId()
  }

  private rebuildFromInstalled(){
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
    if (!this.activeWebspace || this.activeWebspace === 'default') return
    const entry = this.webspaces.find(ws => ws.id === this.activeWebspace)
    const ok = confirm(`Удалить webspace "${entry?.title || this.activeWebspace}"?`)
    if (!ok) return
    try {
      await this.adaos.sendEventsCommand('desktop.webspace.delete', { id: this.activeWebspace })
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

  async resetDb(){
    await this.y.clearStorage()
    location.reload()
  }
}
