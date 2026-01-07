import { Injectable } from '@angular/core'
import { ToastController } from '@ionic/angular'
import { ActionConfig } from './page-schema.model'
import { PageStateService } from './page-state.service'
import { AdaosClient } from '../core/adaos/adaos-client.service'
import { YDocService } from '../y/ydoc.service'
import { PageModalService } from './page-modal.service'

export interface ActionContext {
  event?: any
  widget?: any
}

@Injectable({ providedIn: 'root' })
export class PageActionService {
  constructor(
    private state: PageStateService,
    private adaos: AdaosClient,
    private toast: ToastController,
    private ydoc: YDocService,
    private modals: PageModalService
  ) {}

  async handle(action: ActionConfig, ctx: ActionContext = {}): Promise<void> {
    if (!action) return
    if (action.type === 'updateState') {
      const patch = this.resolveParams(action.params ?? {}, ctx)
      this.state.patch(patch)
      return
    }
    if (action.type === 'openModal') {
      const params = this.resolveParams(action.params ?? {}, ctx)
      const modalId = params?.['modalId'] || params?.['id'] || action.target
      await this.modals.openModalById(modalId)
      return
    }
    if (action.type === 'callSkill') {
      await this.callSkill(action, ctx)
      return
    }
    if (action.type === 'openOverlay') {
      await this.openOverlay(action, ctx)
      return
    }
    if (action.type === 'callHost') {
      await this.callHost(action, ctx)
      return
    }
  }

  private async callSkill(
    action: ActionConfig,
    ctx: ActionContext
  ): Promise<void> {
    const target = action.target || ''
    const [skill, method] = target.split('.', 2)
    if (!skill || !method) return
    const body = this.resolveParams(action.params ?? {}, ctx)
    try {
      await this.adaos.callSkill(skill, method, body).toPromise()
    } catch (err) {
      try {
        const t = await this.toast.create({
          message: 'Action failed',
          duration: 1500,
        })
        await t.present()
      } catch {
        console.warn('callSkill failed', err)
      }
    }
  }

  private async openOverlay(
    _action: ActionConfig,
    _ctx: ActionContext
  ): Promise<void> {
    // For desktop pilot we model overlays through state flags and dedicated widgets.
    // This hook is kept for future extension where overlays are opened imperatively.
    return
  }

  private async callHost(
    action: ActionConfig,
    ctx: ActionContext
  ): Promise<void> {
    const target = action.target || ''
    if (!target) return
    const body = this.resolveParams(action.params ?? {}, ctx)
    // Ensure webspace_id is always present so that hub
    // can apply the command to the correct webspace even
    // if the events websocket was registered from another
    // workspace earlier in the session.
    try {
      const anyAdaos: any = this.adaos as any
      const webspaceId =
        typeof anyAdaos.getCurrentWebspaceId === 'function'
          ? anyAdaos.getCurrentWebspaceId()
          : undefined
      if (webspaceId && !body.webspace_id && !body.workspace_id) {
        body.webspace_id = webspaceId
      }
    } catch {
      // best-effort only
    }
    try {
      const ack = await this.adaos.sendEventsCommand(target, body)
      // If we just triggered a webspace reload/reset for the current
      // webspace, drop local IndexedDB snapshot so the next Yjs sync
      // does not re-apply stale ui/application state.
      if (
        (target === 'desktop.webspace.reload' || target === 'desktop.webspace.reset') &&
        (!body.webspace_id || body.webspace_id === this.ydoc.getWebspaceId())
      ) {
        try {
          await this.ydoc.clearStorage()
          // full page reload to re-init YDoc with fresh state
          location.reload()
        } catch {
          // best-effort
        }
      }
      return ack
    } catch (err) {
      try {
        const t = await this.toast.create({
          message: 'Host action failed',
          duration: 1500,
        })
        await t.present()
      } catch {
        console.warn('callHost failed', err)
      }
    }
  }

  private resolveParams(input: any, ctx: ActionContext): any {
    if (!input || typeof input !== 'object') return input
    const state = this.state.getSnapshot()
    const out: any = {}
    for (const [k, v] of Object.entries(input)) {
      if (typeof v === 'string') {
        if (v.startsWith('$state.')) {
          const path = v.slice('$state.'.length)
          out[k] = this.readByPath(state, path)
          continue
        }
        if (v.startsWith('$event.')) {
          const path = v.slice('$event.'.length)
          out[k] = this.readByPath(ctx.event, path)
          continue
        }
      }
      out[k] = v
    }
    return out
  }

  private readByPath(source: any, path: string): any {
    if (!source || !path) return undefined
    return path.split('.').reduce((acc, key) => (acc != null ? (acc as any)[key] : undefined), source)
  }
}
