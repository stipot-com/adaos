import { Component, Input, OnChanges, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { Observable } from 'rxjs'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'

@Component({
  selector: 'ada-details-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `
    <ion-card>
      <ion-card-header *ngIf="widget?.title">
        <ion-card-title>{{ widget.title }}</ion-card-title>
      </ion-card-header>
      <ion-card-content>
        <pre *ngIf="data$ | async as value">{{ value | json }}</pre>
      </ion-card-content>
    </ion-card>
  `,
})
export class DetailsWidgetComponent implements OnInit, OnChanges {
  @Input() widget!: WidgetConfig

  data$?: Observable<any>

  constructor(private data: PageDataService) {}

  ngOnInit(): void {
    this.updateStream()
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.updateStream()
  }

  private updateStream(): void {
    const ds = this.widget?.dataSource
    this.data$ = ds ? this.data.load<any>(ds) : undefined
  }
}

