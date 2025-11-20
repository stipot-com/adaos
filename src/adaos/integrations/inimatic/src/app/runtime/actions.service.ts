import { Injectable } from '@angular/core'
import { ModalController, ToastController } from '@ionic/angular'

@Injectable({ providedIn: 'root' })
export class ActionsService {
  constructor(private modal: ModalController, private toast: ToastController) {}

  async run(action: any, ctx: any = {}) {
    if (!action) return
    if (action.openModal) {
      ctx.onOpenModal?.(action.openModal)
      return
    }
    if (action.toast) {
      const t = await this.toast.create({ message: action.toast, duration: 1500 })
      await t.present()
      return
    }
  }
}

