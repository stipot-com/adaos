import { Injectable } from '@angular/core'
import { ModalController } from '@ionic/angular'
import { YDocService } from '../y/ydoc.service'
import { AdaosClient } from '../core/adaos/adaos-client.service'
import { ModalHostComponent } from '../renderer/modals/modal.component'
import type { AdaModalConfig } from './dsl-types'

type ModalConfig = AdaModalConfig

@Injectable()
export class PageModalService {
  constructor(
    private modalCtrl: ModalController,
    private ydoc: YDocService,
    private adaos: AdaosClient
  ) {}

  async openModalById(modalId?: string): Promise<void> {
    if (!modalId) return
    const modals = this.ydoc.toJSON(this.ydoc.getPath('ui/application/modals')) || {}
    const modalCfg: ModalConfig | undefined = modals[modalId] || this.resolveStaticModal(modalId)
    if (!modalCfg) return
    if (modalCfg.schema) {
      await this.openSchemaModal(modalCfg)
      return
    }
    if (modalCfg.type === 'catalog-apps' || modalCfg.type === 'catalog-widgets') {
      await this.openCatalogModal(modalCfg, modalId)
      return
    }
    await this.openSimpleModal(modalCfg)
  }

  private async openCatalogModal(modalCfg: ModalConfig, modalId: string): Promise<void> {
    const type = modalCfg.type
    const itemsPath = type === 'catalog-apps' ? 'data/catalog/apps' : 'data/catalog/widgets'
    const installedPath = type === 'catalog-apps' ? 'data/installed/apps' : 'data/installed/widgets'
    const items = this.ydoc.toJSON(this.ydoc.getPath(itemsPath)) || []
    const installedInitial: string[] = this.ydoc.toJSON(this.ydoc.getPath(installedPath)) || []
    const installed = new Set(installedInitial)
    const kind: 'app' | 'widget' = type === 'catalog-apps' ? 'app' : 'widget'

    const modal = await this.modalCtrl.create({
      component: ModalHostComponent,
      componentProps: {
        type: modalCfg.type,
        cfg: {
          title: modalCfg.title,
          items,
          isInstalled: (it: any) => installed.has(it?.id),
          toggle: (it: any) => {
            const id = it?.id
            if (!id) return
            if (installed.has(id)) {
              installed.delete(id)
            } else {
              installed.add(id)
            }
            this.toggleInstall(kind, id)
          },
          close: () => modal.dismiss(),
        },
      },
    })
    await modal.present()
  }

  private isInstalled(path: string, id: string): boolean {
    if (!id) return false
    const list: string[] = this.ydoc.toJSON(this.ydoc.getPath(path)) || []
    return list.includes(id)
  }

  private async toggleInstall(kind: 'app' | 'widget', id?: string): Promise<void> {
    if (!id) return
    try {
      await this.adaos.sendEventsCommand('desktop.toggleInstall', { type: kind, id })
    } catch (err) {
      console.warn('desktop.toggleInstall failed', err)
    }
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
    const modal = await this.modalCtrl.create({
      component: ModalHostComponent,
      componentProps: {
        // Тип здесь служит лишь ключом по умолчанию; рендерер
        // переключится на schema-режим, увидев cfg.schema.
        type: modalCfg.type || 'schema',
        cfg: {
          title: modalCfg.title,
          schema: modalCfg.schema,
        },
      },
    })
    await modal.present()
  }
}
