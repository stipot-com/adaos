import { Component, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule, ModalController } from '@ionic/angular'
import { YDocService } from '../../y/ydoc.service'
import { observeDeep } from '../../y/y-helpers'
import { AdaApp } from '../../runtime/dsl-types'
import { WeatherModalComponent } from '../modals/weather-modal.component'
import { WeatherWidgetComponent } from '../widgets/weather-widget.component'

@Component({
  selector: 'ada-desktop',
  standalone: true,
  imports: [CommonModule, IonicModule, WeatherWidgetComponent],
  templateUrl: './desktop.component.html',
  styleUrls: ['./desktop.component.scss']
})
export class DesktopRendererComponent implements OnInit, OnDestroy {
  app?: AdaApp
  dispose?: () => void
  constructor(private y: YDocService, private modal: ModalController) {}

  async ngOnInit() {
    await this.y.initFromSeedIfEmpty()
    const appNode = this.y.getPath('ui/application')
    const update = () => this.app = this.y.toJSON(appNode)
    update()
    this.dispose = observeDeep(appNode, update)
  }
  ngOnDestroy(){ this.dispose?.() }

  async openModal(id: string) {
    const modalCfg = this.app?.modals?.[id]
    if (!modalCfg) return
    if (modalCfg.type === 'weather') {
      const data = this.y.toJSON(this.y.getPath((modalCfg.source||'').replace('y:','')))
      const m = await this.modal.create({ component: WeatherModalComponent, componentProps: { title: modalCfg.title, weather: data }})
      await m.present()
    }
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
}

