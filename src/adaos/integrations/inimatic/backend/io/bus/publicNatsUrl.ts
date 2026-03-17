const DEFAULT_PUBLIC_NATS_WS_URL = 'wss://nats.inimatic.com/nats' as const
const DEFAULT_PUBLIC_NATS_PATH = '/nats' as const

function withLeadingSlash(value: string | undefined, fallback: string): string {
	const trimmed = String(value || '').trim()
	if (!trimmed) {
		return fallback
	}
	return trimmed.startsWith('/') ? trimmed : `/${trimmed}`
}

function normalizePublicNatsWsUrl(
	value: string | undefined,
	{ defaultPath }: { defaultPath: string }
): string | null {
	const trimmed = String(value || '').trim()
	if (!trimmed) {
		return null
	}
	try {
		const parsed = new URL(trimmed.includes('://') ? trimmed : `wss://${trimmed}`)
		if (parsed.protocol === 'http:') {
			parsed.protocol = 'ws:'
		} else if (parsed.protocol === 'https:') {
			parsed.protocol = 'wss:'
		} else if (parsed.protocol !== 'ws:' && parsed.protocol !== 'wss:') {
			return null
		}
		if (!parsed.pathname || parsed.pathname === '/') {
			parsed.pathname = defaultPath
		}
		return parsed.toString()
	} catch {
		return null
	}
}

function derivePublicNatsWsUrlFromRootBase(
	value: string | undefined,
	{ defaultPath }: { defaultPath: string }
): string | null {
	const trimmed = String(value || '').trim()
	if (!trimmed) {
		return null
	}
	try {
		const parsed = new URL(trimmed)
		if (parsed.protocol === 'http:') {
			parsed.protocol = 'ws:'
		} else if (parsed.protocol === 'https:') {
			parsed.protocol = 'wss:'
		} else if (parsed.protocol !== 'ws:' && parsed.protocol !== 'wss:') {
			return null
		}
		parsed.pathname = defaultPath
		parsed.search = ''
		parsed.hash = ''
		return parsed.toString()
	} catch {
		return null
	}
}

export function buildPublicNatsWsUrl(): string {
	const publicPath = withLeadingSlash(process.env['WS_NATS_PATH'], DEFAULT_PUBLIC_NATS_PATH)
	const explicit =
		normalizePublicNatsWsUrl(process.env['NATS_PUBLIC_WS_URL'], { defaultPath: publicPath }) ||
		normalizePublicNatsWsUrl(process.env['PUBLIC_HUB_NATS_WS_URL'], { defaultPath: publicPath })
	if (explicit) {
		return explicit
	}
	const fromHost = normalizePublicNatsWsUrl(process.env['NATS_WS_HOST'], { defaultPath: publicPath })
	if (fromHost) {
		return fromHost
	}
	const fromRootBase = derivePublicNatsWsUrlFromRootBase(process.env['ROOT_BASE_URL'], { defaultPath: publicPath })
	if (fromRootBase) {
		return fromRootBase
	}
	return DEFAULT_PUBLIC_NATS_WS_URL
}
