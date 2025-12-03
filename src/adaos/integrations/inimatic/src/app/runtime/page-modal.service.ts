import { Injectable } from '@angular/core'
import { ModalController } from '@ionic/angular'
import { YDocService } from '../y/ydoc.service'
import { AdaosClient } from '../core/adaos/adaos-client.service'
import { ModalHostComponent } from '../renderer/modals/modal.component'
import type { AdaModalConfig } from './dsl-types'

type ModalConfig = AdaModalConfig

@Injectable({ providedIn: 'root' })
export class PageModalService {
  constructor(
    private modalCtrl: ModalController,
    private ydoc: YDocService,
    private adaos: AdaosClient
  ) {}

  async openModalById(modalId?: string): Promise<void> {
    if (!modalId) return
    // 1) Primary source: projected application modals
    const appModals = this.ydoc.toJSON(this.ydoc.getPath('ui/application/modals')) || {}

    // 2) Fallback: modals defined inside the current scenario section
    let scenarioModals: Record<string, any> = {}
    try {
      const currentScenario = this.ydoc.toJSON(
        this.ydoc.getPath('ui/current_scenario')
      ) as string | undefined
      const scenarioId = currentScenario || 'web_desktop'
      const scenNode: any = this.ydoc.getPath(`ui/scenarios/${scenarioId}`)
      const scenRaw = this.ydoc.toJSON(scenNode) as any
      if (scenRaw && typeof scenRaw === 'object') {
        scenarioModals =
          (scenRaw.application && scenRaw.application.modals) ||
          scenRaw.modals ||
          {}
      }
    } catch {
      scenarioModals = {}
    }

    const modalCfg: ModalConfig | undefined =
      appModals[modalId] || scenarioModals[modalId] || this.resolveStaticModal(modalId)
    if (!modalCfg) return
    if (modalCfg.schema) {
      await this.openSchemaModal(modalCfg)
      return
    }
    await this.openSimpleModal(modalCfg)
  }

  private async openSimpleModal(modalCfg: ModalConfig): Promise<void> {
    const data = modalCfg.source ? this.loadSource(modalCfg.source) : undefined
    const modal = await this.modalCtrl.create({
      component: ModalHostComponent,
      componentProps: {
        type: modalCfg.type || 'modal',
        cfg: {
          title: modalCfg.title,
          data,
        },
      },
    })
    await modal.present()
  }

  private loadSource(source: string): any {
    if (source.startsWith('y:')) {
      const path = source.slice(2)
      return this.ydoc.toJSON(this.ydoc.getPath(path))
    }
    return undefined
  }

  private resolveStaticModal(modalId: string): ModalConfig | undefined {
    if (modalId === 'workspace_manager') {
      return { type: 'workspace-manager', title: 'Workspaces' }
    }
    return undefined
  }

  private async openSchemaModal(modalCfg: ModalConfig): Promise<void> {
    if (!modalCfg.schema) return
    const { SchemaModalComponent } = await import(
      '../renderer/modals/schema-modal.component'
    )
    const modal = await this.modalCtrl.create({
      component: SchemaModalComponent,
      componentProps: {
        title: modalCfg.title,
        schema: modalCfg.schema,
      },
    })
    await modal.present()
  }
}
