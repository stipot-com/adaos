import { Injectable } from '@angular/core'
import { ModalController } from '@ionic/angular/standalone'
import { YDocService } from '../y/ydoc.service'
import { AdaosClient } from '../core/adaos/adaos-client.service'
import { ModalHostComponent } from '../renderer/modals/modal.component'
import type { AdaModalConfig } from './dsl-types'
import type { PageSchema } from './page-schema.model'
import { catchError } from 'rxjs/operators'
import { firstValueFrom, of } from 'rxjs'
import { NotificationLogService } from './notification-log.service'

type ModalConfig = AdaModalConfig

@Injectable({ providedIn: 'root' })
export class PageModalService {
  private readonly catalogPreparationTasks = new Map<'apps_catalog' | 'widgets_catalog', Promise<void>>()

  constructor(
    private modalCtrl: ModalController,
    private ydoc: YDocService,
    private adaos: AdaosClient,
    private notifications: NotificationLogService,
  ) {}

  async openModalById(modalId?: string): Promise<void> {
    if (!modalId) return
    const staticModal = this.resolveStaticModal(modalId)
    if (staticModal) {
      if (modalId === 'apps_catalog' || modalId === 'widgets_catalog') {
        this.queueDesktopCatalogPreparation(modalId)
      }
      if (staticModal.schema) {
        await this.openSchemaModal(staticModal)
        return
      }
      await this.openSimpleModal(staticModal)
      return
    }
    const modalCfg =
      this.readModalConfig(`ui/application/modals/${modalId}`)
      || this.readScenarioModalConfig(modalId)
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
    if (modalId === 'notification_history') {
      return { type: 'notification-history', title: 'Notifications' }
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

  private readModalConfig(path: string): ModalConfig | undefined {
    const raw = this.ydoc.toJSON(this.ydoc.getPath(path))
    if (!raw || typeof raw !== 'object') return undefined
    return raw as ModalConfig
  }

  private readScenarioModalConfig(modalId: string): ModalConfig | undefined {
    try {
      const currentScenario = this.ydoc.toJSON(
        this.ydoc.getPath('ui/current_scenario')
      ) as string | undefined
      const scenarioId = String(currentScenario || 'web_desktop').trim() || 'web_desktop'
      return (
        this.readModalConfig(`ui/scenarios/${scenarioId}/application/modals/${modalId}`)
        || this.readModalConfig(`ui/scenarios/${scenarioId}/modals/${modalId}`)
      )
    } catch {
      return undefined
    }
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

  private queueDesktopCatalogPreparation(modalId: 'apps_catalog' | 'widgets_catalog'): void {
    if (this.catalogPreparationTasks.has(modalId)) return
    const task = this.prepareDesktopCatalogModal(modalId)
      .catch(() => {})
      .finally(() => {
        this.catalogPreparationTasks.delete(modalId)
      })
    this.catalogPreparationTasks.set(modalId, task)
  }

  private async prepareDesktopCatalogModal(modalId: 'apps_catalog' | 'widgets_catalog'): Promise<void> {
    const kind = modalId === 'apps_catalog' ? 'apps' : 'widgets'
    const before = this.ydoc.getMaterializationSnapshot()
    const localReady = kind === 'apps' ? before.hasCatalogApps : before.hasCatalogWidgets
    if (before.ready && localReady) return
    const localModalAvailable = kind === 'apps' ? before.hasAppsCatalogModal : before.hasWidgetsCatalogModal

    const webspaceId = this.adaos.getCurrentWebspaceId?.() || this.ydoc.getWebspaceId() || 'default'
    try {
      const recovered = await this.ydoc.waitForMaterializedDesktopContent(2200)
      if (recovered) {
        const afterRecovered = this.ydoc.getMaterializationSnapshot()
        const recoveredLocalReady =
          kind === 'apps' ? afterRecovered.hasCatalogApps : afterRecovered.hasCatalogWidgets
        if (recoveredLocalReady) return
      }
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
    const unsupportedByScenario = !localModalAvailable && !localReady && catalogCount <= 0
    await this.notifications.show(
      unsupportedByScenario
        ? `${kind === 'apps' ? 'Apps' : 'Widgets'} is not exposed by the current scenario (${currentScenario}).`
        : `${kind === 'apps' ? 'Apps' : 'Widgets'} catalog is opening in degraded mode. ` +
          `Yjs materialization is incomplete: scenario=${currentScenario}, ` +
          `pageSchema=${after.hasDesktopPageSchema ? 'yes' : 'no'}, ` +
          `catalog.${kind}=${kind === 'apps' ? (after.hasCatalogApps ? 'yes' : 'no') : (after.hasCatalogWidgets ? 'yes' : 'no')}. ` +
          (remoteReady
            ? `Using control API fallback (${catalogCount} items visible on hub).`
            : 'Hub diagnostics also report incomplete materialization.'),
      {
        duration: 3600,
        position: 'bottom',
        color: unsupportedByScenario ? 'medium' : remoteReady ? 'warning' : 'danger',
        source: 'modal.catalog',
      },
    )
  }
}
