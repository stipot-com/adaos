// backend/io/root/settings.ts
import { readFileSync } from 'node:fs'
import { Agent, type Dispatcher } from 'undici'

export type RootSettings = {
	baseUrl: string
	tgPairingPath: string
	enableMtls: boolean
	mtls?: { certPath: string; keyPath: string; caPath?: string; rejectUnauthorized?: boolean }
}

export function loadRootSettings(): RootSettings | null {
	const baseUrl = process.env['ROOT_BASE_URL']
	const tgPairingPath = process.env['ROOT_TG_PAIRING_PATH']
	if (!baseUrl || !tgPairingPath) return null
	const enableMtls = (process.env['ROOT_MTLS_ENABLED'] || 'false').toLowerCase() === 'true'
	let mtls: RootSettings['mtls']
	if (enableMtls) {
		const certPath = process.env['ROOT_MTLS_CERT_PATH']
		const keyPath = process.env['ROOT_MTLS_KEY_PATH']
		const caPath = process.env['ROOT_MTLS_CA_PATH']
		if (!certPath || !keyPath) throw new Error('ROOT_MTLS_ENABLED=true but cert/key paths are missing')
		mtls = { certPath, keyPath, caPath, rejectUnauthorized: true }
	}
	return { baseUrl, tgPairingPath, enableMtls, mtls }
}

export function makeRootDispatcher(s: RootSettings | null): Dispatcher | undefined {
	if (!s || !s.enableMtls || !s.mtls) return undefined
	const cert = readFileSync(s.mtls.certPath)
	const key = readFileSync(s.mtls.keyPath)
	const ca = s.mtls.caPath ? readFileSync(s.mtls.caPath) : undefined
	const rejectUnauthorized = true
	return new Agent({ connect: { cert, key, ca, rejectUnauthorized } })
}
