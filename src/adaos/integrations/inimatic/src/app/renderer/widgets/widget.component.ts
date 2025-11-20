import { Component, Input, OnChanges, OnInit, ViewChild, ViewContainerRef, ComponentRef } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { WidgetRegistry } from '../../runtime/registry'

@Component({
  selector: 'ada-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `<ng-template #vc></ng-template>`
})
export class WidgetComponent implements OnChanges, OnInit {
  @Input() type!: string
  @Input() cfg: any
  @ViewChild('vc', { read: ViewContainerRef, static: true }) vc!: ViewContainerRef
  private ref?: ComponentRef<any>

  ngOnInit(){ this.render() }
  ngOnChanges(){ this.render() }

  private render(){
    if (!this.vc) return
    this.vc.clear(); this.ref?.destroy()
    const render = WidgetRegistry[this.type]
    if (!render) return
    const out = render(this.cfg) || undefined
    if (!out?.component) return
    this.ref = this.vc.createComponent(out.component)
    if (out.inputs) Object.assign(this.ref.instance, out.inputs)
    try { this.ref.changeDetectorRef.detectChanges() } catch {}
  }
}
