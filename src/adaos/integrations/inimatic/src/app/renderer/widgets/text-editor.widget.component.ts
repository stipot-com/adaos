import { Component, Input, OnChanges, OnInit, SimpleChanges } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { Observable } from 'rxjs'
import { WidgetConfig } from '../../runtime/page-schema.model'
import { PageDataService } from '../../runtime/page-data.service'

@Component({
  selector: 'ada-text-editor-widget',
  standalone: true,
  imports: [CommonModule, IonicModule],
  template: `
    <ion-card>
      <ion-card-header *ngIf="widget?.title">
        <ion-card-title>{{ widget.title }}</ion-card-title>
      </ion-card-header>
      <ion-card-content>
        <ng-container *ngIf="content$ | async as value">
          <ion-textarea
            [autoGrow]="true"
            [value]="value"
            readonly="true"
          ></ion-textarea>
        </ng-container>
      </ion-card-content>
    </ion-card>
  `,
})
export class TextEditorWidgetComponent implements OnInit, OnChanges {
  @Input() widget!: WidgetConfig

  content$?: Observable<string | undefined>

  constructor(private data: PageDataService) {}

  ngOnInit(): void {
    this.updateStream()
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.updateStream()
  }

  private updateStream(): void {
    const ds = this.widget?.dataSource
    if (!ds) {
      this.content$ = undefined
      return
    }
    const bindField: string =
      (this.widget.inputs && this.widget.inputs['bindField']) || 'content'
    this.content$ = this.data.load<any>(ds).pipe(
      // map inline to avoid importing operators; small helper
      // eslint-disable-next-line rxjs/finnish
      (source) =>
        new Observable<string | undefined>((subscriber) => {
          const sub = source.subscribe({
            next: (value) => {
              try {
                const next =
                  value && typeof value === 'object' ? (value as any)[bindField] : undefined
                subscriber.next(typeof next === 'string' ? next : undefined)
              } catch {
                subscriber.next(undefined)
              }
            },
            error: (err) => subscriber.error(err),
            complete: () => subscriber.complete(),
          })
          return () => sub.unsubscribe()
        }),
    )
  }
}

