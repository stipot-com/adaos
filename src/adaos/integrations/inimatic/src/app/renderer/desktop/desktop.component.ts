import { Component, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule, ModalController } from '@ionic/angular'
import { YDocService } from '../../y/ydoc.service'
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
  resolvedIcons: Array<{ id:string; title:string; icon:string; action?: any }> = []
  resolvedWidgets: Array<{ id:string; type:string; title?:string; source?:string }> = []
  constructor(private y: YDocService, private modal: ModalController) {}

  async ngOnInit() {
    await this.y.initFromHub()
    const appNode = this.y.getPath('ui/application')
    const dataNode = this.y.getPath('data')
    const recompute = () => {
      this.app = this.y.toJSON(appNode)
      this.rebuildFromInstalled()
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
      const installedPathPrimary = type === 'catalog-apps' ? 'data/desktop/installed/apps' : 'data/desktop/installed/widgets'
      const installedPathFallback = type === 'catalog-apps' ? 'data/installed/apps' : 'data/installed/widgets'
      const itemsPath = type === 'catalog-apps' ? 'data/catalog/apps' : 'data/catalog/widgets'
      const items = this.y.toJSON(this.y.getPath(itemsPath)) || []
      let installed: string[] = (this.y.toJSON(this.y.getPath(installedPathPrimary))
        || this.y.toJSON(this.y.getPath(installedPathFallback))
        || []) as string[]

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
          const desktopCur = this.y.toJSON(dataMap.get('desktop')) || {}
          const nextDesktop = {
            ...desktopCur,
            installed: {
              apps: (type === 'catalog-apps') ? next : (desktopCur.installed?.apps || installed),
              widgets: (type === 'catalog-widgets') ? next : (desktopCur.installed?.widgets || installed)
            }
          }
          dataMap.set('desktop', nextDesktop)
        })
        installed = next
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

  private rebuildFromInstalled(){
    // resolve icons from data.desktop.installed.apps (fallback data.installed.apps) + data.catalog.apps
    const catalogApps: any[] = this.y.toJSON(this.y.getPath('data/catalog/apps')) || []
    const installedApps: string[] = this.y.toJSON(this.y.getPath('data/desktop/installed/apps'))
      || this.y.toJSON(this.y.getPath('data/installed/apps')) || []
    const byId: Record<string, any> = {}
    for (const it of catalogApps) byId[it.id] = it
    this.resolvedIcons = installedApps
      .map(id => byId[id])
      .filter(Boolean)
      .map(it => ({ id: it.id, title: it.title || it.id, icon: it.icon || (this.app as any)?.desktop?.iconTemplate?.icon || 'apps-outline', action: it.launchModal ? { openModal: it.launchModal } : undefined }))

    // resolve widgets from data.desktop.installed.widgets (fallback data.installed.widgets) + data.catalog.widgets
    const catalogWidgets: any[] = this.y.toJSON(this.y.getPath('data/catalog/widgets')) || []
    const installedWidgets: string[] = this.y.toJSON(this.y.getPath('data/desktop/installed/widgets'))
      || this.y.toJSON(this.y.getPath('data/installed/widgets')) || []
    const wById: Record<string, any> = {}
    for (const it of catalogWidgets) wById[it.id] = it
    this.resolvedWidgets = installedWidgets
      .map(id => wById[id])
      .filter(Boolean)
      .map(it => ({ id: it.id, type: it.type, title: it.title, source: it.source }))
  }

  async resetDb(){
    await this.y.clearStorage()
    location.reload()
  }
}
