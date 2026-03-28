// src\adaos\integrations\inimatic\src\app\renderer\modals\workspace-manager-modal.component.ts
import { Component, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule, ToastController, ModalController } from '@ionic/angular'
import { FormsModule } from '@angular/forms'
import { YDocService } from '../../y/ydoc.service'
import { AdaosClient } from '../../core/adaos/adaos-client.service'
import { observeDeep } from '../../y/y-helpers'
import { firstValueFrom } from 'rxjs'

type WebspaceEntry = {
  id: string
  title: string
  created_at?: number
  kind?: string
  home_scenario?: string
  source_mode?: string
}

type ScenarioOption = {
  id: string
  title: string
}

@Component({
  selector: 'ada-workspace-manager-modal',
  standalone: true,
  imports: [CommonModule, IonicModule, FormsModule],
  template: `
    <ion-header>
      <ion-toolbar>
        <ion-title>Workspaces</ion-title>
        <ion-buttons slot="end">
          <ion-button (click)="dismiss()">Close</ion-button>
        </ion-buttons>
      </ion-toolbar>
    </ion-header>
    <ion-content [scrollY]="true" class="ion-padding-bottom">
      <div class="form">
        <ion-card>
          <ion-card-header>
            <ion-card-title>Existing</ion-card-title>
          </ion-card-header>
          <ion-card-content>
            <ion-item>
              <ion-select
                label="Webspace"
                labelPlacement="floating"
                [value]="selectedWorkspaceId"
                (ionChange)="onSelectedWorkspaceChange($event.detail.value)"
              >
                <ion-select-option *ngFor="let ws of webspaces" [value]="ws.id">
                  {{ ws.title || ws.id }}
                </ion-select-option>
              </ion-select>
            </ion-item>
            <ion-item>
              <ion-input
                label="Title"
                labelPlacement="floating"
                [(ngModel)]="selectedWorkspaceTitle"
              ></ion-input>
            </ion-item>
            <ion-item>
              <ion-select
                label="Home scenario"
                labelPlacement="floating"
                [value]="selectedWorkspaceHomeScenario"
                (ionChange)="selectedWorkspaceHomeScenario = normalizeScenarioId($event.detail.value)"
              >
                <ion-select-option *ngFor="let scenario of availableScenarios" [value]="scenario.id">
                  {{ scenario.title }}
                </ion-select-option>
              </ion-select>
            </ion-item>
            <div class="workspace-meta" *ngIf="selectedWorkspace as ws">
              <div><strong>Kind:</strong> {{ formatWorkspaceKind(ws) }}</div>
              <div><strong>Home scenario:</strong> {{ ws.home_scenario || 'web_desktop' }}</div>
              <div><strong>Source mode:</strong> {{ ws.source_mode || 'workspace' }}</div>
            </div>
            <ion-button
              expand="block"
              fill="outline"
              (click)="goHomeSelected()"
              [disabled]="!selectedWorkspaceId"
            >
              Go Home In Selected Space
            </ion-button>
            <ion-button
              expand="block"
              fill="outline"
              (click)="setSelectedHomeToCurrentScenario()"
              [disabled]="!canSetSelectedHomeToCurrentScenario()"
            >
              Make Current Scenario Home
            </ion-button>
            <ion-button expand="block" (click)="saveSelectedMetadata()">
              Save Metadata
            </ion-button>
            <ion-button
              expand="block"
              fill="clear"
              color="danger"
              (click)="deleteSelected()"
              [disabled]="selectedWorkspaceId === 'default' || selectedWorkspaceId === 'desktop'"
            >
              Delete
            </ion-button>
            <ion-button expand="block" fill="clear" (click)="refresh()">Refresh</ion-button>
          </ion-card-content>
        </ion-card>

        <ion-card>
          <ion-card-header>
            <ion-card-title>Create New</ion-card-title>
          </ion-card-header>
          <ion-card-content>
            <ion-item>
              <ion-input label="ID" labelPlacement="floating" [(ngModel)]="newWorkspaceId"></ion-input>
            </ion-item>
            <ion-item>
              <ion-input label="Title" labelPlacement="floating" [(ngModel)]="newWorkspaceTitle"></ion-input>
            </ion-item>
            <ion-item>
              <ion-select
                label="Scenario"
                labelPlacement="floating"
                [value]="newWorkspaceScenarioId"
                (ionChange)="newWorkspaceScenarioId = normalizeScenarioId($event.detail.value)"
              >
                <ion-select-option *ngFor="let scenario of availableScenarios" [value]="scenario.id">
                  {{ scenario.title }}
                </ion-select-option>
              </ion-select>
            </ion-item>
            <ion-item lines="none">
              <ion-checkbox
                labelPlacement="start"
                [(ngModel)]="newWorkspaceDev"
              >
                Dev workspace
              </ion-checkbox>
            </ion-item>
            <div class="workspace-meta workspace-meta-create">
              <div><strong>Kind:</strong> {{ newWorkspaceDev ? 'dev' : 'workspace' }}</div>
              <div><strong>Home scenario:</strong> {{ previewHomeScenario() }}</div>
              <div><strong>Source mode:</strong> {{ newWorkspaceDev ? 'dev' : 'workspace' }}</div>
            </div>
            <ion-button expand="block" (click)="createWorkspace()">Create</ion-button>
          </ion-card-content>
        </ion-card>
      </div>
    </ion-content>
  `,
  styles: [
    `
      ion-content {
        --padding-start: 16px;
        --padding-end: 16px;
        --padding-bottom: 24px;
      }
      .form {
        margin-top: 16px;
        margin-bottom: 16px;
        display: flex;
        flex-direction: column;
        gap: 12px;
        padding-bottom: env(safe-area-inset-bottom, 0px);
      }
      ion-card:last-child {
        margin-bottom: 0;
      }
      .workspace-meta {
        padding: 8px 4px 12px;
        display: grid;
        gap: 4px;
        font-size: 13px;
        opacity: 0.82;
      }
      .workspace-meta strong {
        opacity: 0.94;
      }
      .workspace-meta-create {
        padding-top: 2px;
      }
    `,
  ],
})
export class WorkspaceManagerModalComponent implements OnInit, OnDestroy {
  webspaces: WebspaceEntry[] = []
  availableScenarios: ScenarioOption[] = []
  activeWebspace = ''
  newWorkspaceId = ''
  newWorkspaceTitle = ''
  newWorkspaceScenarioId = ''
  newWorkspaceDev = false
  selectedWorkspaceId = ''
  selectedWorkspaceTitle = ''
  selectedWorkspaceHomeScenario = ''

  private dispose?: () => void

  constructor(
    private ydoc: YDocService,
    private adaos: AdaosClient,
    private toast: ToastController,
    private modalCtrl: ModalController
  ) {}

  async ngOnInit(): Promise<void> {
    this.activeWebspace = this.ydoc.getWebspaceId()
    const node: any = this.ydoc.getPath('data/webspaces')
    const recompute = () => {
      const raw = this.ydoc.toJSON(node)
      this.webspaces = Array.isArray(raw?.items) ? raw.items : []
      if (!this.selectedWorkspaceId) {
        this.applySelection(this.activeWebspace || this.webspaces[0]?.id)
      } else if (!this.webspaces.find((ws) => ws.id === this.selectedWorkspaceId)) {
        this.applySelection(this.activeWebspace || this.webspaces[0]?.id)
      } else {
        this.applySelection(this.selectedWorkspaceId, false)
      }
    }
    this.dispose = observeDeep(node, recompute)
    recompute()
    if (!this.webspaces.length) {
      await this.loadWebspacesFallback()
    }
    await this.loadScenarioOptions()
    if (!this.newWorkspaceScenarioId) {
      this.newWorkspaceScenarioId = this.readCurrentScenarioId()
    }
  }

  ngOnDestroy(): void {
    this.dispose?.()
  }

  async onSelectedWorkspaceChange(id: string): Promise<void> {
    this.applySelection(id)
    await this.switchWorkspace(id)
  }

  dismiss(): void {
    this.modalCtrl.dismiss()
  }

  async createWorkspace(): Promise<void> {
    const id = this.newWorkspaceId.trim()
    if (!id) {
      await this.presentToast('Specify workspace ID')
      return
    }
    const title = this.newWorkspaceTitle.trim() || id
    const scenarioId = this.previewHomeScenario()
    const payload: Record<string, any> = {
      id,
      title,
      scenario_id: scenarioId,
      dev: this.newWorkspaceDev,
    }
    try {
      await this.adaos.sendEventsCommand('desktop.webspace.create', payload)
      this.newWorkspaceId = ''
      this.newWorkspaceTitle = ''
      this.newWorkspaceScenarioId = this.readCurrentScenarioId()
      this.newWorkspaceDev = false
      await this.switchWorkspace(id)
    } catch {
      try {
        const response = await this.postNode('/api/node/yjs/webspaces', payload)
        const createdId = String(response?.webspace?.id || id).trim() || id
        this.newWorkspaceId = ''
        this.newWorkspaceTitle = ''
        this.newWorkspaceScenarioId = this.readCurrentScenarioId()
        this.newWorkspaceDev = false
        await this.loadWebspacesFallback()
        await this.switchWorkspace(createdId)
      } catch {
        await this.presentToast('Failed to create workspace')
      }
    }
  }

  async refresh(): Promise<void> {
    try {
      await this.adaos.sendEventsCommand('desktop.webspace.refresh', {})
      await this.loadWebspacesFallback()
    } catch {
      await this.loadWebspacesFallback()
      if (!this.webspaces.length) {
        await this.presentToast('Failed to refresh list')
      }
    }
  }

  async saveSelectedMetadata(): Promise<void> {
    const id = (this.selectedWorkspaceId || '').trim()
    if (!id) return
    const title = (this.selectedWorkspaceTitle || '').trim() || id
    const homeScenario = this.normalizeScenarioId(this.selectedWorkspaceHomeScenario)
    try {
      await this.adaos.sendEventsCommand('desktop.webspace.update', {
        id,
        title,
        home_scenario: homeScenario,
      })
    } catch {
      try {
        await this.postNode(`/api/node/yjs/webspaces/${encodeURIComponent(id)}`, {
          title,
          home_scenario: homeScenario,
        }, 'patch')
        await this.loadWebspacesFallback()
        this.applySelection(id)
      } catch {
        await this.presentToast('Failed to update workspace')
      }
    }
  }

  async deleteSelected(): Promise<void> {
    const id = (this.selectedWorkspaceId || '').trim()
    if (!id || id === 'default' || id === 'desktop') return
    try {
      await this.adaos.sendEventsCommand('desktop.webspace.delete', { id })
      if (id === this.activeWebspace) {
        const fallback = this.webspaces.find((ws) => ws.id !== id)?.id || 'default'
        await this.switchWorkspace(fallback)
      } else {
        this.applySelection(this.activeWebspace)
      }
    } catch {
      await this.presentToast('Failed to delete workspace')
    }
  }

  async goHomeSelected(): Promise<void> {
    const id = (this.selectedWorkspaceId || '').trim()
    if (!id) return
    const returnWebspaceId =
      id === this.activeWebspace ? this.ydoc.getReturnWebspaceId(id) : undefined
    if (returnWebspaceId && returnWebspaceId !== id) {
      try {
        await this.switchWorkspace(returnWebspaceId)
        return
      } catch {
        await this.presentToast('Failed to switch to return workspace')
        return
      }
    }
    try {
      await this.adaos.sendEventsCommand('desktop.webspace.go_home', { webspace_id: id })
      await this.switchWorkspace(id)
    } catch {
      try {
        await this.postNodeWebspaceAction(
          `/api/node/yjs/webspaces/${encodeURIComponent(id)}/go-home`,
          {}
        )
        await this.switchWorkspace(id)
      } catch {
        await this.presentToast('Failed to go home in selected workspace')
      }
    }
  }

  async setSelectedHomeToCurrentScenario(): Promise<void> {
    const id = (this.selectedWorkspaceId || '').trim()
    if (!this.canSetSelectedHomeToCurrentScenario() || !id) return
    const scenarioId = this.readCurrentScenarioId()
    try {
      await this.adaos.sendEventsCommand('desktop.webspace.set_home', {
        webspace_id: id,
        scenario_id: scenarioId,
      })
    } catch {
      try {
        await this.postNodeWebspaceAction(
          `/api/node/yjs/webspaces/${encodeURIComponent(id)}/set-home`,
          { scenario_id: scenarioId }
        )
        const entry = this.selectedWorkspace
        if (entry) {
          entry.home_scenario = scenarioId
        }
        this.selectedWorkspaceHomeScenario = scenarioId
      } catch {
        await this.presentToast('Failed to update home scenario')
      }
    }
  }

  get selectedWorkspace(): WebspaceEntry | undefined {
    return this.webspaces.find((ws) => ws.id === this.selectedWorkspaceId)
  }

  private applySelection(id?: string, updateTitle = true): void {
    this.selectedWorkspaceId = id || ''
    const entry = this.webspaces.find((ws) => ws.id === id)
    if (updateTitle) {
      this.selectedWorkspaceTitle = entry?.title || id || ''
    }
    this.selectedWorkspaceHomeScenario = this.normalizeScenarioId(entry?.home_scenario || '') || 'web_desktop'
    this.ensureScenarioOption(this.selectedWorkspaceHomeScenario)
  }

  private async switchWorkspace(id: string): Promise<void> {
    if (!id || id === this.activeWebspace) return
    try {
      await this.ydoc.switchWebspace(id)
      this.activeWebspace = id
    } catch {
      await this.presentToast('Failed to switch workspace')
    }
  }

  private async presentToast(message: string): Promise<void> {
    const toast = await this.toast.create({ message, duration: 2000 })
    await toast.present()
  }

  private async loadWebspacesFallback(): Promise<void> {
    try {
      const response = await firstValueFrom(
        this.adaos.get<{ ok?: boolean; accepted?: boolean; items?: WebspaceEntry[] }>('/api/node/yjs/webspaces')
      )
      const items = Array.isArray(response?.items) ? response.items : []
      if (!items.length) return
      this.webspaces = items
      if (!this.selectedWorkspaceId || !this.webspaces.find((ws) => ws.id === this.selectedWorkspaceId)) {
        this.applySelection(this.activeWebspace || this.webspaces[0]?.id)
      } else {
        this.applySelection(this.selectedWorkspaceId, false)
      }
    } catch {
      // best-effort fallback when current YDoc does not expose data.webspaces yet
    }
  }

  private async postNode(path: string, body: Record<string, any>, method: 'post' | 'patch' = 'post'): Promise<any> {
    const response =
      method === 'patch'
        ? await firstValueFrom(this.adaos.patch<any>(path, body))
        : await firstValueFrom(this.adaos.post<any>(path, body))
    if (response?.accepted === false || response?.ok === false) {
      throw new Error(String(response?.error || 'host_action_rejected'))
    }
    return response
  }

  private async postNodeWebspaceAction(path: string, body: Record<string, any>): Promise<any> {
    return this.postNode(path, body)
  }

  private async loadScenarioOptions(): Promise<void> {
    const currentScenarioId = this.readCurrentScenarioId()
    const options: ScenarioOption[] = []
    const seen = new Set<string>()
    const push = (rawId: unknown, rawTitle?: unknown) => {
      const id = this.normalizeScenarioId(rawId)
      if (!id || seen.has(id)) return
      seen.add(id)
      options.push({
        id,
        title: String(rawTitle || id).trim() || id,
      })
    }
    push(currentScenarioId, currentScenarioId)
    try {
      const raw = this.ydoc.toJSON(this.ydoc.getPath('ui/scenarios'))
      if (raw && typeof raw === 'object') {
        for (const [id, value] of Object.entries(raw as Record<string, any>)) {
          const title =
            value?.application?.title ||
            value?.application?.name ||
            id
          push(id, title)
        }
      }
    } catch {}
    try {
      const response = await firstValueFrom(this.adaos.get<{ items?: Array<Record<string, any>> }>('/api/scenarios/list'))
      const items = Array.isArray(response?.items) ? response.items : []
      for (const item of items) {
        push(item?.['name'] || item?.['id'], item?.['name'] || item?.['id'])
      }
    } catch {
      // best-effort only
    }
    this.availableScenarios = options.sort((a, b) => a.title.localeCompare(b.title))
    this.ensureScenarioOption(this.selectedWorkspaceHomeScenario)
    this.ensureScenarioOption(this.newWorkspaceScenarioId || currentScenarioId)
  }

  private ensureScenarioOption(scenarioId?: string): void {
    const normalized = this.normalizeScenarioId(scenarioId)
    if (!normalized) return
    if (this.availableScenarios.some((item) => item.id === normalized)) return
    this.availableScenarios = [
      ...this.availableScenarios,
      { id: normalized, title: normalized },
    ].sort((a, b) => a.title.localeCompare(b.title))
  }

  formatWorkspaceKind(entry?: WebspaceEntry): string {
    return String(entry?.kind || '').trim() || 'workspace'
  }

  canSetSelectedHomeToCurrentScenario(): boolean {
    return Boolean(this.selectedWorkspaceId) && this.selectedWorkspaceId === this.activeWebspace
  }

  previewHomeScenario(): string {
    return this.normalizeScenarioId(this.newWorkspaceScenarioId) || (this.newWorkspaceDev ? this.readCurrentScenarioId() : 'web_desktop')
  }

  normalizeScenarioId(value: any): string {
    const scenarioId = String(value || '').trim()
    return scenarioId || 'web_desktop'
  }

  private readCurrentScenarioId(): string {
    try {
      const raw = this.ydoc.toJSON(this.ydoc.getPath('ui/current_scenario'))
      if (typeof raw === 'string' && raw.trim()) {
        return raw.trim()
      }
    } catch {}
    return 'web_desktop'
  }
}
