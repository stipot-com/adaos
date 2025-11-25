import { Component, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { HttpClient } from '@angular/common/http'
import { PageSchema, WidgetConfig } from '../../runtime/page-schema.model'
import { PageStateService } from '../../runtime/page-state.service'
import { PageWidgetHostComponent } from '../widgets/page-widget-host.component'
import { YDocService } from '../../y/ydoc.service'
import '../../runtime/registry.weather'
import '../../runtime/registry.catalogs'
import '../../runtime/registry.workspaces'

@Component({
  selector: 'ada-dynamic-desktop-page',
  standalone: true,
  imports: [CommonModule, IonicModule, PageWidgetHostComponent],
  template: `
    <ion-content>
      <div class="desktop-page">
        <ng-container *ngIf="schema">
          <!-- Step 1: topbar + workspace tools -->
          <ng-container *ngFor="let widget of topbarWidgets">
            <ada-page-widget-host [widget]="widget"></ada-page-widget-host>
          </ng-container>
          <!-- Step 2: icons grid -->
          <ng-container *ngFor="let widget of iconWidgets">
            <ada-page-widget-host [widget]="widget"></ada-page-widget-host>
          </ng-container>
          <!-- Step 3: widgets summary list -->
          <ng-container *ngFor="let widget of widgetSummaryWidgets">
            <ada-page-widget-host [widget]="widget"></ada-page-widget-host>
          </ng-container>
          <!-- Step 4: weather summary tile -->
          <ng-container *ngFor="let widget of weatherSummaryWidgets">
            <ada-page-widget-host [widget]="widget"></ada-page-widget-host>
          </ng-container>
        </ng-container>
      </div>
    </ion-content>
  `,
  styles: [
    `
      .desktop-page {
        display: flex;
        flex-direction: column;
        gap: 8px;
        padding: 8px;
      }
    `,
  ],
})
export class DynamicDesktopPageComponent implements OnInit {
  schema?: PageSchema

  constructor(
    private http: HttpClient,
    private state: PageStateService,
    private ydoc: YDocService
  ) {}

  async ngOnInit(): Promise<void> {
    try {
      // eslint-disable-next-line no-console
      console.log('[DynamicDesktop] ngOnInit: initFromHub()...')
    } catch {}
    await this.ydoc.initFromHub()
    try {
      // eslint-disable-next-line no-console
      console.log('[DynamicDesktop] ngOnInit: YDoc ready, loading schema...')
    } catch {}
    this.loadSchema()
  }

  private loadSchemaFromYDoc(): void {
    // 1) Пробуем взять schema из ui/application/desktop/pageSchema (если когда-то будет проецироваться туда).
    try {
      const appNode: any = this.ydoc.getPath('ui/application/desktop/pageSchema')
      const raw = this.ydoc.toJSON(appNode)
      if (raw && typeof raw === 'object') {
        this.schema = raw as PageSchema
        // eslint-disable-next-line no-console
        console.log(
          '[DynamicDesktop] schema loaded from ui/application/desktop/pageSchema',
          this.schema?.id,
          Array.isArray(this.schema?.widgets)
            ? `widgets=${this.schema.widgets.length}`
            : 'widgets=0'
        )
        return
      }
    } catch {
      // ignore and try scenarios fallback
    }

    // 2) Fallback: читаем schema из ui/scenarios/<current_scenario>.application.desktop.pageSchema,
    //    где <current_scenario> обычно "web_desktop".
    try {
      const currentScenario = this.ydoc.toJSON(
        this.ydoc.getPath('ui/current_scenario')
      ) as string | undefined
      const scenarioId = currentScenario || 'web_desktop'
      const scenNode: any = this.ydoc.getPath(`ui/scenarios/${scenarioId}`)
      const scenRaw = this.ydoc.toJSON(scenNode) as any
      if (scenRaw && typeof scenRaw === 'object') {
        const fromScenario =
          scenRaw.application?.desktop?.pageSchema ||
          scenRaw.pageSchema
        if (fromScenario && typeof fromScenario === 'object') {
          this.schema = fromScenario as PageSchema
          // eslint-disable-next-line no-console
          console.log(
            '[DynamicDesktop] schema loaded from data/scenarios',
            scenarioId,
            this.schema?.id,
            Array.isArray(this.schema?.widgets)
              ? `widgets=${this.schema.widgets.length}`
              : 'widgets=0'
          )
          return
        }
      }
      // eslint-disable-next-line no-console
      console.log(
        '[DynamicDesktop] no pageSchema in ui/application or data/scenarios'
      )
      // Для отладки: дамп текущего состояния YDoc.
      this.ydoc.dumpSnapshot()
    } catch (err) {
      try {
        // eslint-disable-next-line no-console
        console.log('[DynamicDesktop] failed to read schema from scenarios', err)
      } catch {}
    }
  }

  private loadSchema(): void {
    const s: PageSchema = {
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
          },
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
          },
          actions: [
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
          },
        },
      ],
    }
    this.schema = s
    try {
      // eslint-disable-next-line no-console
      console.log(
        '[DynamicDesktop] schema loaded (static)',
        this.schema.id,
        `widgets=${this.schema.widgets.length}`
      )
    } catch {}
  }

  get topbarWidgets(): WidgetConfig[] {
    if (!this.schema) return []
    return this.schema.widgets.filter(
      (w) => w.id === 'topbar' || w.id === 'workspace-tools'
    )
  }

  get iconWidgets(): WidgetConfig[] {
    if (!this.schema) return []
    return this.schema.widgets.filter((w) => w.id === 'desktop-icons')
  }

  get widgetSummaryWidgets(): WidgetConfig[] {
    if (!this.schema) return []
    return this.schema.widgets.filter((w) => w.id === 'desktop-widgets')
  }

  get weatherSummaryWidgets(): WidgetConfig[] {
    if (!this.schema) return []
    return this.schema.widgets.filter((w) => w.id === 'weather-summary')
  }

  widgetsInArea(areaId: string): WidgetConfig[] {
    if (!this.schema) return []
    return this.schema.widgets.filter((w) => w.area === areaId)
  }
}
