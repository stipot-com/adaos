// src\adaos\integrations\inimatic\src\app\renderer\modals\schema-modal.component.ts
import { Component, Input } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule, ModalController } from '@ionic/angular'
import { PageSchema } from '../../runtime/page-schema.model'
import { PageWidgetHostComponent } from '../widgets/page-widget-host.component'

@Component({
  selector: 'ada-schema-modal',
  standalone: true,
  imports: [CommonModule, IonicModule, PageWidgetHostComponent],
  template: `
    <ion-header *ngIf="title">
      <ion-toolbar>
        <ion-title>{{ title }}</ion-title>
        <ion-buttons slot="end">
          <ion-button (click)="dismiss()">Close</ion-button>
        </ion-buttons>
      </ion-toolbar>
    </ion-header>
    <ion-content>
      <div class="schema-modal">
        <ng-container *ngIf="schema">
          <ng-container *ngFor="let widget of schema.widgets">
            <ada-page-widget-host [widget]="widget"></ada-page-widget-host>
          </ng-container>
        </ng-container>
      </div>
    </ion-content>
  `,
  styles: [
    `
      :host {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      ion-content {
        --padding-start: 12px;
        --padding-end: 12px;
        --padding-top: 12px;
        --padding-bottom: 12px;
      }
      .schema-modal {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
    `,
  ],
})
export class SchemaModalComponent {
  @Input() title?: string
  @Input() schema?: PageSchema

  constructor(private modalCtrl: ModalController) {}

  dismiss(): void {
    this.modalCtrl.dismiss()
  }
}

