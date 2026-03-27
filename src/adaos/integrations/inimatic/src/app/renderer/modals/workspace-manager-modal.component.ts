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
            <ion-button expand="block" (click)="renameSelected()">
              Save Title
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
  activeWebspace = ''
  newWorkspaceId = ''
  newWorkspaceTitle = ''
  newWorkspaceDev = false
  selectedWorkspaceId = ''
  selectedWorkspaceTitle = ''

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
    try {
      const payload: Record<string, any> = {
        id,
        title,
        dev: this.newWorkspaceDev,
      }
      if (this.newWorkspaceDev) {
        payload['scenario_id'] = this.readCurrentScenarioId()
      }
      await this.adaos.sendEventsCommand('desktop.webspace.create', payload)
      this.newWorkspaceId = ''
      this.newWorkspaceTitle = ''
      this.newWorkspaceDev = false
      await this.switchWorkspace(id)
    } catch {
      await this.presentToast('Failed to create workspace')
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

  async renameSelected(): Promise<void> {
    const id = (this.selectedWorkspaceId || '').trim()
    if (!id) return
    const title = (this.selectedWorkspaceTitle || '').trim() || id
    try {
      await this.adaos.sendEventsCommand('desktop.webspace.rename', { id, title })
    } catch {
      await this.presentToast('Failed to rename workspace')
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
      await this.presentToast('Failed to go home in selected workspace')
    }
  }

  async setSelectedHomeToCurrentScenario(): Promise<void> {
    const id = (this.selectedWorkspaceId || '').trim()
    if (!this.canSetSelectedHomeToCurrentScenario() || !id) return
    try {
      await this.adaos.sendEventsCommand('desktop.webspace.set_home', {
        webspace_id: id,
        scenario_id: this.readCurrentScenarioId(),
      })
    } catch {
      await this.presentToast('Failed to update home scenario')
    }
  }

  get selectedWorkspace(): WebspaceEntry | undefined {
    return this.webspaces.find((ws) => ws.id === this.selectedWorkspaceId)
  }

  private applySelection(id?: string, updateTitle = true): void {
    this.selectedWorkspaceId = id || ''
    if (updateTitle) {
      const entry = this.webspaces.find((ws) => ws.id === id)
      this.selectedWorkspaceTitle = entry?.title || id || ''
    }
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

  formatWorkspaceKind(entry?: WebspaceEntry): string {
    return String(entry?.kind || '').trim() || 'workspace'
  }

  canSetSelectedHomeToCurrentScenario(): boolean {
    return Boolean(this.selectedWorkspaceId) && this.selectedWorkspaceId === this.activeWebspace
  }

  previewHomeScenario(): string {
    return this.newWorkspaceDev ? this.readCurrentScenarioId() : 'web_desktop'
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
