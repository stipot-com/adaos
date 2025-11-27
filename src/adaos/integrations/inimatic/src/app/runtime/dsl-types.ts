// src\adaos\integrations\inimatic\src\app\runtime\dsl-types.ts
import { PageSchema } from './page-schema.model'

export type CatalogItem = { id: string; title?: string; icon?: string; type?: string; source?: string; launchModal?: string }

export type AdaModalConfig = {
	title: string
	type?: string
	source?: string
	schema?: PageSchema
}

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
	modals: Record<string, AdaModalConfig>
	registry?: { widgets?: string[]; modals?: string[] }
}
