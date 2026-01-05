import { Component, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { PageSchema, WidgetConfig } from '../../runtime/page-schema.model'
import { PageWidgetHostComponent } from '../widgets/page-widget-host.component'
import { YDocService } from '../../y/ydoc.service'
import { DesktopSchemaService } from '../../runtime/desktop-schema.service'
import { AdaApp } from '../../runtime/dsl-types'
import '../../runtime/registry.workspaces'

@Component({
  selector: 'ada-dynamic-desktop-page',
  standalone: true,
  imports: [CommonModule, IonicModule, PageWidgetHostComponent],
  template: `
    <ion-content [style.--background]="background">
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
  background = 'var(--ion-background-color)'

  constructor(
    private ydoc: YDocService,
    private schemaService: DesktopSchemaService
  ) {}

  async ngOnInit(): Promise<void> {
    try {
      // eslint-disable-next-line no-console
      console.log('[DynamicDesktop] ngOnInit: initFromHub()...')
    } catch {}
    await this.ydoc.initFromHub()
    this.readBackground()
    this.loadSchema()
  }

  private loadSchema(): void {
    try {
      this.schema = this.schemaService.loadSchema()
      try {
        // eslint-disable-next-line no-console
        console.log(
          '[DynamicDesktop] schema loaded',
          this.schema?.id,
          Array.isArray(this.schema?.widgets)
            ? `widgets=${this.schema.widgets.length}`
            : 'widgets=0'
        )
      } catch {}
    } catch (err) {
      this.schema = undefined
      try {
        // eslint-disable-next-line no-console
        console.log('[DynamicDesktop] failed to load schema', err)
      } catch {}
    }
  }

  private readBackground(): void {
    try {
      const app = this.ydoc.toJSON(
        this.ydoc.getPath('ui/application')
      ) as AdaApp | undefined
      this.background = app?.desktop?.background || 'var(--ion-background-color)'
    } catch {
      this.background = 'var(--ion-background-color)'
    }
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

