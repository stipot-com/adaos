export type AdaApp = {
  version: string
  desktop: {
    background?: string
    icons: Array<{ id: string; title: string; icon: string; action?: any }>
    widgets?: Array<{ id: string; type: string; title?: string; source?: string }>
  }
  modals: Record<string, { title: string; type: string; source: string }>
}

