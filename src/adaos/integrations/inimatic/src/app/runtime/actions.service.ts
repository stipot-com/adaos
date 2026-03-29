import { Injectable } from '@angular/core'
import { NotificationLogService } from './notification-log.service'

@Injectable({ providedIn: 'root' })
export class ActionsService {
  constructor(private notifications: NotificationLogService) {}

  async run(action: any, ctx: any = {}) {
    if (!action) return
    if (action.openModal) {
      ctx.onOpenModal?.(action.openModal)
      return
    }
    if (action.toast) {
      await this.notifications.show(String(action.toast || ''), {
        duration: 1500,
        source: 'action.schema',
      })
      return
    }
  }
}

