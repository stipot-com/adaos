import { Component, Input, OnChanges, OnDestroy, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { QRCodeModule } from 'angularx-qrcode'
import { Subscription } from 'rxjs'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'

@Component({
  selector: 'ada-qr-code-widget',
  standalone: true,
  imports: [CommonModule, IonicModule, QRCodeModule],
  template: `
    <ion-card>
      <ion-card-header *ngIf="widget?.title">
        <ion-card-title>{{ widget.title }}</ion-card-title>
      </ion-card-header>
      <ion-card-content>
        <div class="qr-wrap" *ngIf="qrData; else emptyState">
          <qrcode
            [qrdata]="qrData"
            [width]="width"
            [errorCorrectionLevel]="'M'"
          ></qrcode>
          <div class="qr-caption" *ngIf="caption">{{ caption }}</div>
        </div>
        <ng-template #emptyState>
          <div class="qr-empty">{{ emptyText }}</div>
        </ng-template>
      </ion-card-content>
    </ion-card>
  `,
  styles: [
    `
      .qr-wrap {
        display: flex;
        flex-direction: column;
        gap: 12px;
        align-items: center;
        text-align: center;
      }
      .qr-caption {
        font-size: 12px;
        opacity: 0.75;
        word-break: break-word;
      }
      .qr-empty {
        font-size: 13px;
        opacity: 0.75;
      }
    `,
  ],
})
export class QrCodeWidgetComponent implements OnInit, OnChanges, OnDestroy {
  @Input() widget!: WidgetConfig

  qrData = ''
  caption = ''
  width = 240
  emptyText = 'No QR code yet'

  private dataSub?: Subscription

  constructor(private data: PageDataService) {}

  ngOnInit(): void {
    this.updateStream()
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.updateStream()
  }

  ngOnDestroy(): void {
    this.dataSub?.unsubscribe()
  }

  private updateStream(): void {
    this.dataSub?.unsubscribe()
    this.width = Number(this.widget?.inputs?.['width'] || 240) || 240
    this.emptyText = String(this.widget?.inputs?.['emptyText'] || 'No QR code yet')
    const bindField = String(this.widget?.inputs?.['bindField'] || 'qr_text')
    const captionField = String(this.widget?.inputs?.['captionField'] || 'code')
    const ds = this.widget?.dataSource
    if (!ds) {
      this.qrData = ''
      this.caption = ''
      return
    }
    this.dataSub = this.data.load<any>(ds).subscribe({
      next: (value) => {
        const obj = value && typeof value === 'object' ? value : {}
        const qrValue = (obj as any)[bindField]
        const captionValue = (obj as any)[captionField]
        this.qrData = typeof qrValue === 'string' ? qrValue : ''
        this.caption = typeof captionValue === 'string' ? captionValue : ''
      },
      error: () => {
        this.qrData = ''
        this.caption = ''
      },
    })
  }
}
