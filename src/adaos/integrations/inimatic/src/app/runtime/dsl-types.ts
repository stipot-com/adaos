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

// ---------------------------------------------------------------------------
// UI language sources (for scenarios/skills)
// ---------------------------------------------------------------------------

export type AdaUiLang = 'en' | 'ru' | 'fr' | 'ch'
export type AdaUiDictionary = Record<string, string>

// A pluggable translation source. Scenarios/skills can expose one via
// `globalThis.__ADAOS_LANGUAGE_SOURCE__` or by calling `I18nService.registerSource(...)`.
export interface AdaUiLanguageSource {
	getDictionary(lang: AdaUiLang): AdaUiDictionary | Promise<AdaUiDictionary> | null | undefined
}
