// src\adaos\integrations\inimatic\src\app\runtime\page-schema.model.ts
export type WidgetType =
  | 'collection.grid'
  | 'collection.tree'
  | 'visual.metricTile'
  | 'feedback.log'
  | 'feedback.statusBar'
  | 'ui.chat'
  | 'ui.nluTeacher'
  | 'ui.voiceInput'
  | 'ui.list'
  | 'ui.form'
  | 'ui.actions'
  | 'item.textEditor'
  | 'item.codeViewer'
  | 'item.details'
  | 'input.commandBar'
  | 'input.text'
  | 'input.selector'
  | 'desktop.widgets'
  | 'host.webspaceControls'

export interface LayoutArea {
  id: string
  role?: string
  label?: string
}

export interface LayoutConfig {
  type: 'single' | 'split' | 'custom'
  areas: LayoutArea[]
}

export interface PageSchema {
  id: string
  title?: string
  layout: LayoutConfig
  widgets: WidgetConfig[]
}

export interface WidgetConfig {
  id: string
  type: WidgetType
  area: string
  title?: string
  dataSource?: DataSourceConfig
  inputs?: Record<string, any>
  actions?: ActionConfig[]
  visibleIf?: string
}

export type DataSourceConfig = SkillDataSource | ApiDataSource | StaticDataSource
  | YDocDataSource

export interface SkillDataSource {
  kind: 'skill'
  name: string
  params?: Record<string, any>
}

export interface ApiDataSource {
  kind: 'api'
  url: string
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  params?: Record<string, any>
  body?: any
}

export interface StaticDataSource {
  kind: 'static'
  value: any
}

export interface YDocDataSource {
  kind: 'y'
  path?: string
  transform?: 'desktop.icons' | 'desktop.widgets'
}

export interface ActionConfig {
  on: string
  type: 'callSkill' | 'updateState' | 'openOverlay' | 'openModal' | 'callHost'
  target?: string
  params?: Record<string, any>
}
