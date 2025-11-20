// pairing/api.ts
export function extractStartCode(text: string | undefined | null): string | null {
	const t = (text || '').trim()
	if (!t) return null
	if (t.toLowerCase().startsWith('/start ')) {
		const code = t.split(' ', 2)[1]
		return (code || '').trim() || null
	}
	return null
}

