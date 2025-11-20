export type CatalogItem = { id: string; title?: string; icon?: string; type?: string; source?: string; launchModal?: string }

export type AdaApp = {
  version: string
  desktop: {
    background?: string
    topbar?: Array<{ id: string; label: string; action?: any }>
    iconTemplate?: any
    widgetTemplate?: any
    // legacy fields may still appear in early seeds
    icons?: Array<{ id: string; title: string; icon: string; action?: any }>
    widgets?: Array<{ id: string; type: string; title?: string; source?: string }>
  }
  modals: Record<string, { title: string; type: string; source?: string }>
  registry?: { widgets?: string[]; modals?: string[] }
}
