// Legacy overlay widget was specific to weather and is no longer used.
// Kept as a no-op placeholder to avoid breaking PAGE_WIDGET_REGISTRY.
import { Component, Input } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicStandaloneImports } from '../../shared/ionic-standalone'
import { WidgetConfig } from '../../runtime/page-schema.model'

@Component({
  selector: 'ada-modal-overlay-widget',
  standalone: true,
  imports: [CommonModule, IonicStandaloneImports],
  template: ` <ng-container></ng-container> `,
})
export class ModalOverlayWidgetComponent {
  @Input() widget!: WidgetConfig
}
