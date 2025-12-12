// src\adaos\integrations\inimatic\src\app\renderer\modals\workspace-manager-modal.component.ts
import { Component, OnDestroy, OnInit } from '@angular/core'
import { CommonModule } from '@angular/common'
import { IonicModule, ToastController, ModalController } from '@ionic/angular'
import { FormsModule } from '@angular/forms'
import { YDocService } from '../../y/ydoc.service'
import { AdaosClient } from '../../core/adaos/adaos-client.service'
import { observeDeep } from '../../y/y-helpers'

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
    <ion-content>
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
      }
      .form {
        margin-top: 16px;
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
    `,
  ],
})
export class WorkspaceManagerModalComponent implements OnInit, OnDestroy {
  webspaces: Array<{ id: string; title: string; created_at?: number }> = []
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
      await this.adaos.sendEventsCommand('desktop.webspace.create', {
        id,
        title,
        dev: this.newWorkspaceDev,
      })
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
    } catch {
      await this.presentToast('Failed to refresh list')
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
}
