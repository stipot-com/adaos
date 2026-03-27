import { Injectable } from '@angular/core'
import { ToastController } from '@ionic/angular'
import { ActionConfig } from './page-schema.model'
import { PageStateService } from './page-state.service'
import { AdaosClient } from '../core/adaos/adaos-client.service'
import { YDocService } from '../y/ydoc.service'

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
    private ydoc: YDocService
  ) {}

  async handle(action: ActionConfig, ctx: ActionContext = {}): Promise<void> {
    if (!action) return
    if (action.type === 'updateState') {
      const patch = this.resolveParams(action.params ?? {}, ctx)
      this.state.patch(patch)
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
      if (
        target === 'desktop.scenario.set' &&
        !!body.dev &&
        typeof body.scenario_id === 'string' &&
        body.scenario_id.trim()
      ) {
        const sourceWebspaceId = this.ydoc.getWebspaceId()
        const ensureAck = await this.sendHostCommandWithFallback('desktop.webspace.ensure_dev', {
          scenario_id: body.scenario_id,
          title: typeof body.title === 'string' ? body.title : undefined,
        })
        const ensuredWebspaceId = String(ensureAck?.data?.webspace_id || '').trim()
        if (ensuredWebspaceId) {
          if (sourceWebspaceId && ensuredWebspaceId !== sourceWebspaceId) {
            this.ydoc.rememberReturnWebspace(ensuredWebspaceId, sourceWebspaceId)
          }
          await this.ydoc.switchWebspace(ensuredWebspaceId)
        }
        return ensureAck
      }
      const ack = await this.sendHostCommandWithFallback(target, body)
      if (target === 'desktop.webspace.ensure_dev') {
        const ensuredWebspaceId = String(ack?.data?.webspace_id || '').trim()
        if (ensuredWebspaceId && ensuredWebspaceId !== this.ydoc.getWebspaceId()) {
          await this.ydoc.switchWebspace(ensuredWebspaceId)
        }
      }
      // If we just triggered a webspace reload/reset for the current
      // webspace, drop local IndexedDB snapshot so the next Yjs sync
      // does not re-apply stale ui/application state.
      if (
        (target === 'desktop.webspace.reload' || target === 'desktop.webspace.reset') &&
        (!body.webspace_id || body.webspace_id === this.ydoc.getWebspaceId())
      ) {
        try {
          await new Promise((resolve) => setTimeout(resolve, 1500))
          await this.ydoc.resyncCurrentWebspace({
            reason: 'manual',
            clearLocalCache: true,
            room: this.ydoc.getWebspaceId(),
            waitForFirstSyncTimeoutMs: 10_000,
          })
        } catch {
          // best-effort
        }
      }
      if (
        (target === 'desktop.scenario.set' || target === 'desktop.webspace.go_home') &&
        (!body.webspace_id || body.webspace_id === this.ydoc.getWebspaceId())
      ) {
        const syncRuntime = this.ydoc.getSyncRuntimeSnapshot()
        if (syncRuntime.connectionState !== 'connected') {
          try {
            await new Promise((resolve) => setTimeout(resolve, 500))
            await this.ydoc.resyncCurrentWebspace({
              reason: 'manual',
              room: this.ydoc.getWebspaceId(),
              waitForFirstSyncTimeoutMs: 9000,
            })
          } catch {
            // best-effort
          }
        }
      }
      return ack
    } catch (err) {
      if (await this.recoverKnownHostAction(target, body)) {
        return
      }
      try {
        const t = await this.toast.create({
          message: this.describeHostError(err),
          duration: 2200,
        })
        await t.present()
      } catch {
        console.warn('callHost failed', err)
      }
    }
  }

  private hostCommandTimeoutMs(target: string): number {
    const normalized = String(target || '').trim()
    if (
      normalized === 'desktop.scenario.set' ||
      normalized === 'desktop.webspace.ensure_dev' ||
      normalized === 'desktop.webspace.reload' ||
      normalized === 'desktop.webspace.reset' ||
      normalized === 'desktop.webspace.go_home' ||
      normalized === 'desktop.webspace.set_home' ||
      normalized === 'infrastate.action'
    ) {
      return 12_000
    }
    return 5000
  }

  private resolveHostCommandHttpFallback(
    target: string,
    body: Record<string, any>
  ): { path: string; body?: Record<string, any> } | null {
    const webspaceId = String(
      body?.['webspace_id'] || body?.['workspace_id'] || this.ydoc.getWebspaceId() || 'default'
    ).trim() || 'default'
    const encodedWebspaceId = encodeURIComponent(webspaceId)
    if (target === 'desktop.webspace.ensure_dev') {
      return {
        path: '/api/node/yjs/dev-webspaces/ensure',
        body: {
          scenario_id: body?.['scenario_id'],
          title: body?.['title'],
          requested_id: body?.['requested_id'],
        },
      }
    }
    if (target === 'desktop.scenario.set') {
      return {
        path: `/api/node/yjs/webspaces/${encodedWebspaceId}/scenario`,
        body: {
          scenario_id: body?.['scenario_id'],
          set_home:
            typeof body?.['set_home'] === 'boolean'
              ? body['set_home']
              : typeof body?.['persist_home'] === 'boolean'
                ? body['persist_home']
                : undefined,
        },
      }
    }
    if (target === 'desktop.webspace.reload') {
      return {
        path: `/api/node/yjs/webspaces/${encodedWebspaceId}/reload`,
        body: {
          scenario_id: body?.['scenario_id'],
        },
      }
    }
    if (target === 'desktop.webspace.reset') {
      return {
        path: `/api/node/yjs/webspaces/${encodedWebspaceId}/reset`,
        body: {
          scenario_id: body?.['scenario_id'],
        },
      }
    }
    if (target === 'desktop.webspace.go_home') {
      return {
        path: `/api/node/yjs/webspaces/${encodedWebspaceId}/go-home`,
        body: {},
      }
    }
    if (target === 'desktop.webspace.set_home') {
      return {
        path: `/api/node/yjs/webspaces/${encodedWebspaceId}/set-home`,
        body: {
          scenario_id: body?.['scenario_id'],
        },
      }
    }
    if (target === 'infrastate.action') {
      return {
        path: '/api/node/infrastate/action',
        body: {
          id: body?.['id'],
          webspace_id: webspaceId,
          node_id: body?.['node_id'],
          value: body?.['value'],
        },
      }
    }
    return null
  }

  private async sendHostCommandWithFallback(
    target: string,
    body: Record<string, any>
  ): Promise<any> {
    const timeoutMs = this.hostCommandTimeoutMs(target)
    try {
      return await this.adaos.sendEventsCommand(target, body, timeoutMs)
    } catch (err) {
      const fallback = this.resolveHostCommandHttpFallback(target, body)
      if (!fallback) {
        throw err
      }
      try {
        const response = await this.adaos.post<any>(fallback.path, fallback.body ?? {}).toPromise()
        if (response?.accepted === false || response?.ok === false) {
          throw new Error(String(response?.error || 'host_action_rejected'))
        }
        return response
      } catch (fallbackErr) {
        try {
          ;(fallbackErr as any).__adaosPrimaryError = err
        } catch {}
        throw fallbackErr
      }
    }
  }

  private resolveParams(input: any, ctx: ActionContext): any {
    if (!input || typeof input !== 'object') return input
    const state = this.state.getSnapshot()
    const out: any = {}
    for (const [k, v] of Object.entries(input)) {
      if (typeof v === 'string') {
        if (v === '$event') {
          out[k] = ctx.event
          continue
        }
        if (v === '$state') {
          out[k] = state
          continue
        }
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

  private async recoverKnownHostAction(target: string, body: any): Promise<boolean> {
    if (target !== 'infrastate.action') return false
    const actionId = String(body?.id || '').trim()
    if (actionId !== 'start_update') return false
    try {
      await new Promise((resolve) => setTimeout(resolve, 1200))
      const response = await this.adaos.get<any>('/api/admin/update/status').toPromise()
      const status = response?.status ?? response ?? {}
      const state = String(status?.state || '').trim().toLowerCase()
      const reason = String(status?.reason || '').trim().toLowerCase()
      if (
        reason === 'infrastate.start_update' ||
        ['countdown', 'draining', 'stopping', 'restarting', 'applying', 'validated'].includes(state)
      ) {
        return true
      }
    } catch {
      return false
    }
    return false
  }

  private describeHostError(err: any): string {
    const raw = String(err?.message || err || '').trim()
    if (!raw) return 'Host action failed'
    return `Host action failed: ${raw}`
  }
}
