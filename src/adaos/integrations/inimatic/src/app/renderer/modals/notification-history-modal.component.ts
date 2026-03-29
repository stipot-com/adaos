import { ChangeDetectionStrategy, Component, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule } from '@ionic/angular'
import { ModalController, ToastController } from '@ionic/angular/standalone'
import { NotificationHistoryEntry, NotificationLogService } from '../../runtime/notification-log.service'

@Component({
  selector: 'ada-notification-history-modal',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, IonicModule],
  template: `
    <ion-header>
      <ion-toolbar>
        <ion-title>Notifications</ion-title>
        <ion-buttons slot="end">
          <ion-button (click)="clear()" [disabled]="!entries.length">Clear</ion-button>
          <ion-button (click)="dismiss()">Close</ion-button>
        </ion-buttons>
      </ion-toolbar>
    </ion-header>
    <ion-content>
      <div class="history" *ngIf="entries.length; else emptyState">
        <section class="entry" *ngFor="let entry of entries; trackBy: trackByEntry">
          <div class="entry-head">
            <span class="entry-level" [attr.data-level]="entry.level">{{ entry.level }}</span>
            <span class="entry-meta">{{ formatMeta(entry) }}</span>
            <ion-button fill="clear" size="small" (click)="copy(entry, $event)">Copy</ion-button>
          </div>
          <pre class="entry-message">{{ entry.message }}</pre>
        </section>
      </div>
      <ng-template #emptyState>
        <div class="empty-state">
          <div class="empty-state__title">No notifications yet</div>
          <div class="empty-state__body">
            Recent toast messages and runtime notices will be kept here so they can be read and copied later.
          </div>
        </div>
      </ng-template>
    </ion-content>
  `,
  styles: [
    `
      .history {
        display: flex;
        flex-direction: column;
        gap: 12px;
        padding: 16px;
      }
      .entry {
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 14px;
        background: rgba(255, 255, 255, 0.03);
        padding: 12px;
      }
      .entry-head {
        display: flex;
        align-items: center;
        gap: 10px;
        flex-wrap: wrap;
        margin-bottom: 8px;
      }
      .entry-level {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        border-radius: 999px;
        padding: 2px 8px;
        font-size: 11px;
        text-transform: uppercase;
        border: 1px solid rgba(255, 255, 255, 0.1);
      }
      .entry-level[data-level='success'] {
        background: rgba(var(--ion-color-success-rgb, 45, 211, 111), 0.12);
      }
      .entry-level[data-level='warning'] {
        background: rgba(var(--ion-color-warning-rgb, 255, 196, 9), 0.12);
      }
      .entry-level[data-level='error'] {
        background: rgba(var(--ion-color-danger-rgb, 235, 68, 90), 0.12);
      }
      .entry-meta {
        font-size: 12px;
        opacity: 0.76;
        flex: 1 1 220px;
      }
      .entry-message {
        margin: 0;
        white-space: pre-wrap;
        word-break: break-word;
        user-select: text;
        font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
      }
      .empty-state {
        padding: 24px;
        display: grid;
        gap: 8px;
      }
      .empty-state__title {
        font-weight: 600;
      }
      .empty-state__body {
        opacity: 0.78;
        line-height: 1.45;
      }
    `,
  ],
})
export class NotificationHistoryModalComponent implements OnInit {
  entries: NotificationHistoryEntry[] = []

  constructor(
    private modalCtrl: ModalController,
    private notifications: NotificationLogService,
    private toastCtrl: ToastController,
  ) {}

  ngOnInit(): void {
    this.refresh()
  }

  dismiss(): void {
    this.modalCtrl.dismiss()
  }

  clear(): void {
    this.notifications.clear()
    this.refresh()
  }

  async copy(entry: NotificationHistoryEntry, event?: Event): Promise<void> {
    event?.preventDefault()
    event?.stopPropagation()
    const text = `${entry.ts} [${entry.level}]${entry.source ? ` (${entry.source})` : ''}${entry.code ? ` ${entry.code}` : ''}\n${entry.message}`
    try {
      await navigator.clipboard.writeText(text)
      const toast = await this.toastCtrl.create({
        message: 'Notification copied.',
        duration: 1200,
        position: 'bottom',
        color: 'success',
      })
      await toast.present()
    } catch {
      const toast = await this.toastCtrl.create({
        message: 'Copy failed.',
        duration: 1400,
        position: 'bottom',
        color: 'warning',
      })
      await toast.present()
    }
  }

  formatMeta(entry: NotificationHistoryEntry): string {
    const parts = [entry.ts]
    if (entry.source) parts.push(entry.source)
    if (entry.code) parts.push(entry.code)
    return parts.join(' | ')
  }

  trackByEntry(_index: number, entry: NotificationHistoryEntry): string {
    return entry.id
  }

  private refresh(): void {
    this.entries = this.notifications.getSnapshot()
  }
}
