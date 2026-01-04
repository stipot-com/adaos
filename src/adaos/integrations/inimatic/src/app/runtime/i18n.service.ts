import { Injectable } from '@angular/core'
import { HttpClient } from '@angular/common/http'
import { BehaviorSubject, firstValueFrom } from 'rxjs'
import type { AdaUiDictionary, AdaUiLang, AdaUiLanguageSource } from './dsl-types'

export type UiLang = AdaUiLang
type Dict = AdaUiDictionary

function normalizeLang(raw: string | null | undefined): UiLang {
	const v = String(raw || '').toLowerCase()
	if (v.startsWith('ru')) return 'ru'
	if (v.startsWith('fr')) return 'fr'
	if (v.startsWith('zh') || v.startsWith('ch')) return 'ch'
	return 'en'
}

@Injectable({ providedIn: 'root' })
export class I18nService {
	private readonly langSubject = new BehaviorSubject<UiLang>('en')
	readonly lang$ = this.langSubject.asObservable()

	private readonly dicts = new Map<UiLang, Dict>()
	private readonly sources: AdaUiLanguageSource[] = []
	private activeDict: Dict = {}

	constructor(private http: HttpClient) {
		const stored = (() => {
			try {
				return (localStorage.getItem('adaos_lang') || '').trim()
			} catch {
				return ''
			}
		})()
		if (stored) {
			this.langSubject.next(normalizeLang(stored))
		} else {
			const browser = (() => {
				try {
					const nav: any = navigator
					const first = Array.isArray(nav.languages) && nav.languages.length ? nav.languages[0] : nav.language
					return String(first || '')
				} catch {
					return ''
				}
			})()
			this.langSubject.next(normalizeLang(browser))
		}

		try {
			const maybe = (globalThis as any).__ADAOS_LANGUAGE_SOURCE__ as AdaUiLanguageSource | undefined
			if (maybe && typeof maybe === 'object') this.registerSource(maybe)
		} catch {}

		void this.ensureActiveDict()
	}

	getLang(): UiLang {
		return this.langSubject.value
	}

	setLang(lang: UiLang): void {
		this.langSubject.next(lang)
		try {
			localStorage.setItem('adaos_lang', lang)
		} catch {}
		void this.ensureActiveDict()
	}

	registerSource(source: AdaUiLanguageSource): void {
		this.sources.push(source)
		void this.ensureActiveDict()
	}

	t(key: string, params?: Record<string, any>): string {
		const raw = this.activeDict[key] ?? key
		if (!params) return raw
		return raw.replace(/\{(\w+)\}/g, (_m, name) => {
			const v = params[name]
			return v === undefined || v === null ? '' : String(v)
		})
	}

	private async loadBaseDict(lang: UiLang): Promise<Dict> {
		const cached = this.dicts.get(lang)
		if (cached) return cached
		try {
			const url = `assets/i18n/${lang}.json`
			const dict = await firstValueFrom(this.http.get<Dict>(url))
			this.dicts.set(lang, dict || {})
			return dict || {}
		} catch {
			this.dicts.set(lang, {})
			return {}
		}
	}

	private async resolveSourceDicts(lang: UiLang): Promise<Dict[]> {
		const out: Dict[] = []
		for (const source of this.sources) {
			try {
				const res = source.getDictionary(lang)
				const dict = res instanceof Promise ? await res : res
				if (dict && typeof dict === 'object') out.push(dict)
			} catch {
				// ignore broken sources
			}
		}
		return out
	}

	private async ensureActiveDict(): Promise<void> {
		const lang = this.getLang()
		const baseEn = await this.loadBaseDict('en')
		const baseLang = lang === 'en' ? baseEn : await this.loadBaseDict(lang)
		const sourceDicts = await this.resolveSourceDicts(lang)
		this.activeDict = Object.assign({}, baseEn, baseLang, ...sourceDicts)
	}
}

