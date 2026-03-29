import { Injectable } from '@angular/core'
import { ModalController, ToastController } from '@ionic/angular/standalone'
import { YDocService } from '../y/ydoc.service'
import { AdaosClient } from '../core/adaos/adaos-client.service'
import { ModalHostComponent } from '../renderer/modals/modal.component'
import type { AdaModalConfig } from './dsl-types'
import type { PageSchema } from './page-schema.model'
import { catchError } from 'rxjs/operators'
import { firstValueFrom, of } from 'rxjs'

type ModalConfig = AdaModalConfig

@Injectable({ providedIn: 'root' })
export class PageModalService {
  constructor(
    private modalCtrl: ModalController,
    private ydoc: YDocService,
    private adaos: AdaosClient,
    private toastCtrl: ToastController,
  ) {}

  async openModalById(modalId?: string): Promise<void> {
    if (!modalId) return
    if (modalId === 'apps_catalog' || modalId === 'widgets_catalog') {
      await this.prepareDesktopCatalogModal(modalId)
    }
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

  async openTransientSchemaModal(opts: { title?: string; schema: PageSchema }): Promise<void> {
    const { SchemaModalComponent } = await import(
      '../renderer/modals/schema-modal.component'
    )
    const modal = await this.modalCtrl.create({
      component: SchemaModalComponent,
      componentProps: {
        title: opts.title,
        schema: opts.schema,
      },
    })
    await modal.present()
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
    if (modalId === 'apps_catalog') {
      return {
        title: 'Apps',
        schema: this.buildDesktopCatalogSchema('apps'),
      }
    }
    if (modalId === 'widgets_catalog') {
      return {
        title: 'Widgets',
        schema: this.buildDesktopCatalogSchema('widgets'),
      }
    }
    return undefined
  }

  private async openSchemaModal(modalCfg: ModalConfig): Promise<void> {
    if (!modalCfg.schema) return
    await this.openTransientSchemaModal({
      title: modalCfg.title,
      schema: modalCfg.schema,
    })
  }

  private buildDesktopCatalogSchema(kind: 'apps' | 'widgets'): PageSchema {
    const title = kind === 'apps' ? 'Apps' : 'Widgets'
    return {
      id: `${kind}_catalog`,
      title,
      layout: {
        type: 'single',
        areas: [{ id: 'main', role: 'main' }],
      },
      widgets: [
        {
          id: `${kind}-catalog-grid`,
          type: 'collection.grid',
          area: 'main',
          title,
          dataSource: {
            kind: 'y',
            path: `data/catalog/${kind}`,
          },
          inputs: {
            tileMinWidth: kind === 'apps' ? 164 : 186,
          },
        },
      ],
    }
  }

  private async prepareDesktopCatalogModal(modalId: 'apps_catalog' | 'widgets_catalog'): Promise<void> {
    const kind = modalId === 'apps_catalog' ? 'apps' : 'widgets'
    const before = this.ydoc.getMaterializationSnapshot()
    const localReady = kind === 'apps' ? before.hasCatalogApps : before.hasCatalogWidgets
    if (before.ready && localReady) return

    const webspaceId = this.adaos.getCurrentWebspaceId?.() || this.ydoc.getWebspaceId() || 'default'
    try {
      await this.ydoc.resyncCurrentWebspace({
        reason: 'manual',
        room: webspaceId,
        waitForFirstSyncTimeoutMs: 5000,
      })
      const recovered = await this.ydoc.waitForMaterializedDesktopContent(2500)
      if (recovered) return
    } catch {}

    const remote = await firstValueFrom(
      this.adaos.get<any>(`/api/node/yjs/webspaces/${encodeURIComponent(webspaceId)}`).pipe(
        catchError(() => of(undefined)),
      ),
    )
    const after = this.ydoc.getMaterializationSnapshot()
    const remoteMaterialization = remote?.materialization
    const remoteReady = !!remoteMaterialization?.ready
    const catalogCount = Number(remoteMaterialization?.catalog_counts?.[kind] || 0)
    const currentScenario = after.currentScenario || remoteMaterialization?.current_scenario || '-'
    const toast = await this.toastCtrl.create({
      message:
        `${kind === 'apps' ? 'Apps' : 'Widgets'} catalog is opening in degraded mode. ` +
        `Yjs materialization is incomplete: scenario=${currentScenario}, ` +
        `pageSchema=${after.hasDesktopPageSchema ? 'yes' : 'no'}, ` +
        `catalog.${kind}=${kind === 'apps' ? (after.hasCatalogApps ? 'yes' : 'no') : (after.hasCatalogWidgets ? 'yes' : 'no')}. ` +
        (remoteReady
          ? `Using control API fallback (${catalogCount} items visible on hub).`
          : 'Hub diagnostics also report incomplete materialization.'),
      duration: 3600,
      position: 'bottom',
      color: remoteReady ? 'warning' : 'danger',
    })
    await toast.present()
  }
}
