import { Injectable } from '@angular/core'
import { PageSchema } from './page-schema.model'
import { YDocService } from '../y/ydoc.service'

@Injectable({ providedIn: 'root' })
export class DesktopSchemaService {
  constructor(private ydoc: YDocService) {}

  /**
   * Load desktop PageSchema from YDoc, falling back to a built‑in default.
   *
   * Priority:
   * 1) ui/application/desktop/pageSchema
   * 2) ui/scenarios/<current_scenario>.application.desktop.pageSchema
   *    or ui/scenarios/<current_scenario>.pageSchema
   * 3) static default schema (single-column desktop)
   */
  loadSchema(): PageSchema {
    const fromYDoc = this.loadSchemaFromYDoc()
    if (fromYDoc) {
      return fromYDoc
    }
    return this.buildDefaultSchema()
  }

  private loadSchemaFromYDoc(): PageSchema | undefined {
    // 1) Try ui/application/desktop/pageSchema
    try {
      const appNode: any = this.ydoc.getPath('ui/application/desktop/pageSchema')
      const raw = this.ydoc.toJSON(appNode)
      if (raw && typeof raw === 'object') {
        return raw as PageSchema
      }
    } catch {
      // ignore and try scenarios fallback
    }

    // 2) Fallback: ui/scenarios/<current_scenario>.application.desktop.pageSchema
    //    or ui/scenarios/<current_scenario>.pageSchema (for early seeds).
    try {
      const currentScenario = this.ydoc.toJSON(
        this.ydoc.getPath('ui/current_scenario')
      ) as string | undefined
      const scenarioId = currentScenario || 'web_desktop'
      const scenNode: any = this.ydoc.getPath(`ui/scenarios/${scenarioId}`)
      const scenRaw = this.ydoc.toJSON(scenNode) as any
      if (scenRaw && typeof scenRaw === 'object') {
        const fromScenario =
          scenRaw.application?.desktop?.pageSchema || scenRaw.pageSchema
        if (fromScenario && typeof fromScenario === 'object') {
          return fromScenario as PageSchema
        }
      }
    } catch {
      // ignore and let caller use static default
    }
    return undefined
  }

  /**
   * Static default schema used when YDoc does not yet define one.
   * Mirrors the pilot desktop layout used by DynamicDesktopPageComponent.
   */
  private buildDefaultSchema(): PageSchema {
    const schema: PageSchema = {
      id: 'desktop',
      title: 'Desktop',
      layout: {
        type: 'single',
        areas: [{ id: 'main', role: 'main' }],
      },
      widgets: [
        {
          id: 'topbar',
          type: 'input.commandBar',
          area: 'main',
          dataSource: {
            kind: 'y',
            path: 'ui/application/desktop/topbar',
          } as any,
          actions: [
            {
              on: 'click',
              type: 'openModal',
              params: { modalId: '$event.action.openModal' },
            },
          ],
        },
        {
          id: 'workspace-tools',
          type: 'input.commandBar',
          area: 'main',
          inputs: {
            buttons: [{ id: 'workspace-manager', label: 'Workspaces' }],
          },
          actions: [
            {
              on: 'click:workspace-manager',
              type: 'openModal',
              params: { modalId: 'workspace_manager' },
            },
          ],
        },
        {
          id: 'desktop-icons',
          type: 'collection.grid',
          area: 'main',
          title: 'Icons',
          inputs: { columns: 6 },
          dataSource: {
            kind: 'y',
            transform: 'desktop.icons',
          } as any,
          actions: [
            {
              on: 'select',
              type: 'callHost',
              target: 'desktop.scenario.set',
              params: { scenario_id: '$event.scenario_id' },
            },
            {
              on: 'select',
              type: 'openModal',
              params: { modalId: '$event.action.openModal' },
            },
          ],
        },
        {
          id: 'desktop-widgets',
          type: 'desktop.widgets',
          area: 'main',
          title: 'Widgets',
          dataSource: {
            kind: 'y',
            transform: 'desktop.widgets',
          } as any,
        },
      ],
    }
    return schema
  }
}
