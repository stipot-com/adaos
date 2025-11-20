import { Component, Input } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'

@Component({
  selector: 'ada-catalog-modal',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `
  <ion-header><ion-toolbar>
    <ion-title>{{ title }}</ion-title>
    <ion-buttons slot="end"><ion-button (click)="close()">close</ion-button></ion-buttons>
  </ion-toolbar></ion-header>
  <ion-content class="ion-padding">
    <ion-list>
      <ion-item *ngFor="let it of items" button (click)="toggle(it)">
        <ion-label>{{ it.title || it.id }}</ion-label>
        <ion-badge slot="end" color="primary">{{ isInstalled(it) ? 'Installed' : 'Available' }}</ion-badge>
      </ion-item>
    </ion-list>
  </ion-content>`,
  styles: [
    `:host{display:flex;flex-direction:column;height:100%} ion-content{flex:1 1 auto}`
  ]
})
export class CatalogModalComponent {
  @Input() title = ''
  @Input() items: any[] = []
  @Input() isInstalled!: (it:any)=>boolean
  @Input() toggle!: (it:any)=>void
  @Input() close!: () => void
}
