import { WorkspaceManagerModalComponent } from '../renderer/modals/workspace-manager-modal.component'
import { NotificationHistoryModalComponent } from '../renderer/modals/notification-history-modal.component'

export type WidgetRenderFn = (cfg: any) => { component: any; inputs?: any } | undefined
export type ModalRenderFn = (cfg: any) => { component: any; inputs?: any } | undefined

export const WidgetRegistry: Record<string, WidgetRenderFn> = {}

export const ModalRegistry: Record<string, ModalRenderFn> = {
  // Legacy workspace manager modal. Behaviour is still defined
  // imperatively inside the component; config is passed through as-is
  // so that we can evolve it later to a schema-driven variant.
  'workspace-manager': (cfg: any) => ({
    component: WorkspaceManagerModalComponent,
    inputs: cfg || {},
  }),
  'notification-history': (cfg: any) => ({
    component: NotificationHistoryModalComponent,
    inputs: cfg || {},
  }),
}

