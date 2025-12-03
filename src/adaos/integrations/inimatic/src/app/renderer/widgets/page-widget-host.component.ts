import {
  Component,
  ComponentRef,
  Input,
  OnChanges,
  OnDestroy,
  OnInit,
  SimpleChanges,
  Type,
  ViewChild,
  ViewContainerRef,
} from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { WidgetConfig, WidgetType } from '../../runtime/page-schema.model'
import { CollectionGridWidgetComponent } from './collection-grid.widget.component'
import { CommandBarWidgetComponent } from './command-bar.widget.component'
import { MetricTileWidgetComponent } from './metric-tile.widget.component'
import { SelectorWidgetComponent } from './selector.widget.component'
import { TextEditorWidgetComponent } from './text-editor.widget.component'
import { DetailsWidgetComponent } from './details.widget.component'
import { StatusBarWidgetComponent } from './status-bar.widget.component'
import { CodeViewerWidgetComponent } from './code-viewer.widget.component'
import { TextInputWidgetComponent } from './text-input.widget.component'
import { PageStateService, PageState } from '../../runtime/page-state.service'
import { Subscription } from 'rxjs'
import { DesktopWidgetsWidgetComponent } from './desktop-widgets.widget.component'

export const PAGE_WIDGET_REGISTRY: Record<WidgetType, Type<any>> = {
  'collection.grid': CollectionGridWidgetComponent,
  'collection.tree': CollectionGridWidgetComponent,
  'input.commandBar': CommandBarWidgetComponent,
  'input.selector': SelectorWidgetComponent,
  'visual.metricTile': MetricTileWidgetComponent,
  'feedback.log': MetricTileWidgetComponent,
  'feedback.statusBar': StatusBarWidgetComponent,
  'item.textEditor': TextEditorWidgetComponent,
  'item.codeViewer': CodeViewerWidgetComponent,
  'item.details': DetailsWidgetComponent,
  'desktop.widgets': DesktopWidgetsWidgetComponent,
  'host.webspaceControls': CommandBarWidgetComponent,
  'input.text': TextInputWidgetComponent,
}

@Component({
  selector: 'ada-page-widget-host',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `<ng-template #vc></ng-template>`,
})
export class PageWidgetHostComponent implements OnInit, OnChanges, OnDestroy {
  @Input() widget!: WidgetConfig
  @ViewChild('vc', { read: ViewContainerRef, static: true }) vc!: ViewContainerRef

  private ref?: ComponentRef<any>
  private isVisible = true
  private stateSub?: Subscription

  constructor(private pageState: PageStateService) {}

  ngOnInit(): void {
    this.stateSub = this.pageState.selectAll().subscribe((state) => {
      this.updateVisibility(state)
    })
    this.isVisible = this.evaluateVisibility(this.pageState.getSnapshot())
    this.render()
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.isVisible = this.evaluateVisibility(this.pageState.getSnapshot())
    this.render()
  }

  ngOnDestroy(): void {
    this.stateSub?.unsubscribe()
    this.ref?.destroy()
  }

  private render(): void {
    if (!this.vc || !this.widget) return
    this.vc.clear()
    this.ref?.destroy()
    if (!this.isVisible) {
      return
    }
    const cmp = PAGE_WIDGET_REGISTRY[this.widget.type]
    if (!cmp) return
    try {
      // eslint-disable-next-line no-console
      console.log(
        '[PageWidgetHost] render widget',
        this.widget.id,
        'type=',
        this.widget.type
      )
    } catch {}
    this.ref = this.vc.createComponent(cmp)
    Object.assign(this.ref.instance, { widget: this.widget })
    try {
      this.ref.changeDetectorRef.detectChanges()
    } catch {}
  }

  private updateVisibility(state: PageState): void {
    const nextVisible = this.evaluateVisibility(state)
    if (nextVisible === this.isVisible) return
    this.isVisible = nextVisible
    this.render()
  }

  private evaluateVisibility(state: PageState): boolean {
    const expr = (this.widget?.visibleIf || '').trim()
    if (!expr) return true
    if (expr.startsWith('$state.')) {
      const parts = expr.split('===')
      if (parts.length === 2) {
        const key = parts[0].trim().slice('$state.'.length)
        const rawValue = parts[1].trim()
        const expected = this.parseLiteral(rawValue)
        return state[key] === expected
      }
    }
    return true
  }

  private parseLiteral(raw: string): any {
    if (raw === 'true') return true
    if (raw === 'false') return false
    const quoted = raw.match(/^['"](.+)['"]$/)
    if (quoted) return quoted[1]
    return raw
  }
}
