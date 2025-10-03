import type { Request } from 'express';

import en from './locales/en.json' assert { type: 'json' };
import ru from './locales/ru.json' assert { type: 'json' };

export type Locale = 'en' | 'ru';

const DEFAULT_LOCALE: Locale = 'en';
const SUPPORTED: Locale[] = ['en', 'ru'];
const TRANSLATIONS: Record<Locale, Record<string, unknown>> = {
        en: en as Record<string, unknown>,
        ru: ru as Record<string, unknown>,
};

export type MessageParams = Record<string, string | number>;

function pickLocale(preferred?: string): Locale {
        if (!preferred) {
                return DEFAULT_LOCALE;
        }
        const normalized = preferred.trim().toLowerCase();
        if (!normalized) {
                return DEFAULT_LOCALE;
        }
        for (const locale of SUPPORTED) {
                if (normalized === locale || normalized.startsWith(`${locale}-`)) {
                        return locale;
                }
        }
        return DEFAULT_LOCALE;
}

export function resolveLocale(req: Request): Locale {
        const header = req.header('Accept-Language');
        if (header) {
                const first = header.split(',')[0];
                if (first) {
                        return pickLocale(first);
                }
        }
        const envDefault = process.env['BACKEND_DEFAULT_LOCALE'];
        if (envDefault) {
                return pickLocale(envDefault);
        }
        return DEFAULT_LOCALE;
}

function walk(dict: Record<string, unknown>, key: string): string | undefined {
        const parts = key.split('.');
        let current: unknown = dict;
        for (const part of parts) {
                if (typeof current !== 'object' || current === null) {
                        return undefined;
                }
                current = (current as Record<string, unknown>)[part];
        }
        return typeof current === 'string' ? current : undefined;
}

export function translate(locale: Locale, key: string, params?: MessageParams): string {
        const dictionary = TRANSLATIONS[locale] ?? TRANSLATIONS[DEFAULT_LOCALE];
        const fallback = TRANSLATIONS[DEFAULT_LOCALE];
        const template = walk(dictionary, key) ?? walk(fallback, key) ?? key;
        if (!params) {
                return template;
        }
        return template.replace(/\{\{(\w+)\}\}/g, (match, token) => {
                const value = params[token];
                return value !== undefined ? String(value) : match;
        });
}
