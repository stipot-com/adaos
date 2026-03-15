import { randomBytes } from 'node:crypto'
import { redis } from '../idem/kv.js'

const HUB_NATS_SESSION_PREFIX = 'hub:nats:session:'
const HUB_NATS_SESSION_TTL_SEC = 24 * 60 * 60

type HubNatsSessionRecord = {
	hub_id: string
	nats_user: string
	issued_at: number
	expires_at: number
}

export type IssuedHubNatsSession = {
	hubId: string
	user: string
	token: string
	issuedAt: number
	expiresAt: number
	ttlSec: number
}

export type VerifiedHubNatsSession = {
	hubId: string
	user: string
	token: string
	issuedAt: number
	expiresAt: number
}

function sessionKey(token: string): string {
	return `${HUB_NATS_SESSION_PREFIX}${token}`
}

function normalizeHubUser(userRaw: string): { hubId: string, user: string } | null {
	const raw = String(userRaw || '').trim()
	if (!raw) return null
	if (raw.startsWith('hub_')) {
		const hubId = raw.slice(4).trim()
		if (!hubId) return null
		return { hubId, user: `hub_${hubId}` }
	}
	return { hubId: raw, user: `hub_${raw}` }
}

export async function issueHubNatsSession(hubIdRaw: string): Promise<IssuedHubNatsSession> {
	const hubId = String(hubIdRaw || '').trim()
	if (!hubId) {
		throw new Error('hub_nats_session_missing_hub_id')
	}
	const issuedAt = Math.floor(Date.now() / 1000)
	const expiresAt = issuedAt + HUB_NATS_SESSION_TTL_SEC
	const token = randomBytes(36).toString('base64url')
	const user = `hub_${hubId}`
	const record: HubNatsSessionRecord = {
		hub_id: hubId,
		nats_user: user,
		issued_at: issuedAt,
		expires_at: expiresAt,
	}
	await redis.set(sessionKey(token), JSON.stringify(record), 'EX', HUB_NATS_SESSION_TTL_SEC)
	return {
		hubId,
		user,
		token,
		issuedAt,
		expiresAt,
		ttlSec: HUB_NATS_SESSION_TTL_SEC,
	}
}

export async function verifyHubNatsSession(userRaw: string, tokenRaw: string): Promise<VerifiedHubNatsSession | null> {
	const normalized = normalizeHubUser(userRaw)
	const token = String(tokenRaw || '').trim()
	if (!normalized || !token) {
		return null
	}
	const raw = await redis.get(sessionKey(token))
	if (!raw) {
		return null
	}
	let parsed: HubNatsSessionRecord | null = null
	try {
		parsed = JSON.parse(raw) as HubNatsSessionRecord
	} catch {
		return null
	}
	if (!parsed) {
		return null
	}
	const hubId = String(parsed.hub_id || '').trim()
	const user = String(parsed.nats_user || '').trim()
	const issuedAt = Number(parsed.issued_at || 0)
	const expiresAt = Number(parsed.expires_at || 0)
	if (!hubId || !user || !issuedAt || !expiresAt) {
		return null
	}
	if (hubId !== normalized.hubId || user !== normalized.user) {
		return null
	}
	return {
		hubId,
		user,
		token,
		issuedAt,
		expiresAt,
	}
}
