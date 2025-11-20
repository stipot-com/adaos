export type WidgetRenderFn = (cfg: any) => { component: any; inputs?: any } | undefined
export type ModalRenderFn = (cfg: any) => { component: any; inputs?: any } | undefined

export const WidgetRegistry: Record<string, WidgetRenderFn> = {}
export const ModalRegistry: Record<string, ModalRenderFn> = {}

