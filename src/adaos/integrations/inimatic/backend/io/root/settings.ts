import { readFileSync } from 'node:fs'
import { Agent } from 'undici'

export type RootSettings = {
  baseUrl: string
  tgPairingPath: string
  enableMtls: boolean
  mtls?: { certPath: string; keyPath: string; caPath?: string }
}

export function loadRootSettings(): RootSettings | null {
  const baseUrl = process.env['ROOT_BASE_URL']
  const tgPairingPath = process.env['ROOT_TG_PAIRING_PATH']
  if (!baseUrl || !tgPairingPath) return null
  const enableMtls = (process.env['ROOT_MTLS_ENABLED'] || 'false').toLowerCase() === 'true'
  let mtls: RootSettings['mtls'] | undefined
  if (enableMtls) {
    const certPath = process.env['ROOT_MTLS_CERT_PATH']
    const keyPath = process.env['ROOT_MTLS_KEY_PATH']
    const caPath = process.env['ROOT_MTLS_CA_PATH']
    if (!certPath || !keyPath) {
      throw new Error('ROOT_MTLS_ENABLED=true but cert/key paths are missing')
    }
    mtls = { certPath, keyPath, caPath }
  }
  return { baseUrl, tgPairingPath, enableMtls, mtls }
}

export function makeRootDispatcher(s: RootSettings | null) {
  if (!s || !s.enableMtls) return undefined
  const tls: any = {
    cert: readFileSync(s.mtls!.certPath),
    key: readFileSync(s.mtls!.keyPath),
    rejectUnauthorized: true,
  }
  if (s.mtls!.caPath) tls.ca = readFileSync(s.mtls!.caPath)
  return new Agent({ connect: { tls } })
}

