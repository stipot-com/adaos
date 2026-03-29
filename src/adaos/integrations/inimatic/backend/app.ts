// src\adaos\integrations\inimatic\backend\app.ts
import 'dotenv/config'
import express from 'express'
import cors, { type CorsOptions } from 'cors'
import http from 'node:http'
import https from 'node:https'
import type { Socket as NetSocket } from 'node:net'
import path from 'path'
import type { IncomingMessage } from 'node:http'
import { fetch } from 'undici'
import { v4 as uuidv4 } from 'uuid'
import AdmZip from 'adm-zip'
import { Server, Socket } from 'socket.io'
import { createClient } from 'redis'
import fs from 'node:fs'
import { mkdir, stat, writeFile } from 'node:fs/promises'
import { randomBytes, createHash, createHmac, timingSafeEqual } from 'node:crypto'
import type { PeerCertificate, TLSSocket } from 'node:tls'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import YAML from 'yaml'

import { installAdaosBridge } from './adaos-bridge.js'
import { CertificateAuthority } from './pki.js'
import { ForgeManager, type DraftKind } from './forge.js'
import { getPolicy } from './policy.js'
import {
	resolveLocale,
	translate,
	type Locale,
	type MessageParams,
} from './i18n.js'
import { NatsBus } from './io/bus/nats.js'
import { buildPublicNatsWsUrl } from './io/bus/publicNatsUrl.js'
import { installWsNatsProxy, listActiveHubIds } from './io/bus/wsNatsProxy.js'
import { installTelegramWebhookRoutes } from './io/telegram/webhook.js'
import { ensureSchema as ensureTgSchema } from './db/tg.repo.js'
import { installPairingApi } from './io/pairing/api.js'
import { buildInfo } from './build-info.js'
import { installWebAuthnRoutes, storeDeviceCode } from './webauthn.js'
import { installRootLogCapture, queryRootLogs } from './dev/logs.js'
import { listLogFiles, tailLogFile } from './dev/log_files.js'
import { issueHubNatsSession } from './io/bus/hubNatsSession.js'

type FollowerData = {
	followerName: string
	sessionId: string
}

type Follower = {
	[followerSocketId: string]: string
}

type SessionData = {
	initiatorSocketId: string
	followers: Follower
	timestamp: Date
}

type PublicSessionData = SessionData & {
	type: 'public'
	fileNames: Array<{ fileName: string; timestamp: string }>
}

type PrivateSessionData = SessionData & {
	type: 'private'
}

type UnionSessionData = PublicSessionData | PrivateSessionData

type CommunicationData = {
	isInitiator: boolean
	sessionId: string
	data: any
}

type StreamInfo = {
	stream: fs.WriteStream
	destroyTimeout: NodeJS.Timeout
	timestamp: string
}

type OpenedStreams = {
	[sessionId: string]: {
		[fileName: string]: StreamInfo
	}
}

type ClientIdentity =
	| { type: 'hub'; subnetId: string }
	| { type: 'node'; subnetId: string; nodeId: string }

type OwnerHubRecord = {
	hubId: string
	ownerId: string
	createdAt: Date
	lastSeen: Date
	revoked: boolean
}

type OwnerRecord = {
	ownerId: string
	subject: string | null
	scopes: string[]
	refreshToken: string
	accessToken: string
	accessExpiresAt: Date
	hubs: Map<string, OwnerHubRecord>
	createdAt: Date
	updatedAt: Date
}

type DeviceAuthorization = {
	ownerId: string
	deviceCode: string
	userCode: string
	interval: number
	expiresAt: Date
	approved: boolean
}

type RootMgmntAccessMode = 'open' | 'allowlist' | 'denyall'
type RootMgmntLifecycleState =
	| 'active'
	| 'warm'
	| 'stale'
	| 'dormant'
	| 'retired'
	| 'retire_candidate'
type RootMgmntLlmAccess = 'default' | 'frozen' | 'blocked'

type RootMgmntPolicyState = {
	llm_enabled: boolean
	access_mode: RootMgmntAccessMode
	default_model: string
	allowed_models: string[]
	allowed_subnets: string[]
	updated_at: string
	updated_by: string
}

type RootMgmntSubnetOverride = {
	lifecycle_state?: RootMgmntLifecycleState
	llm_access?: RootMgmntLlmAccess
	note?: string
	archived_at?: string
	updated_at: string
	updated_by: string
}

type RootMgmntState = {
	version: 1
	updated_at: string
	policy: RootMgmntPolicyState
	subnets: Record<string, RootMgmntSubnetOverride>
}

type RootMgmntAuditEvent = {
	id: string
	created_at: string
	kind: string
	action?: string
	subnet_id?: string | null
	node_id?: string | null
	actor?: string
	status?: string
	summary?: string
	detail?: Record<string, unknown>
}

type RootMgmntForgeStats = {
	dev_nodes: number
	uploads: number
	draft_artifacts: number
	registry_artifacts: number
	skill_drafts: number
	scenario_drafts: number
	skill_registry_versions: number
	scenario_registry_versions: number
}

type RootMgmntLlmLastSeen = {
	last_request_at?: string
	last_model?: string
	last_status?: string
}

type RootMgmntCaller = {
	subnetId: string | null
	nodeId: string | null
	source: 'mtls' | 'header' | 'unknown'
}

type RootMgmntLlmDecision = {
	allowed: boolean
	status: number
	code?: string
	reason?: string
	state: RootMgmntState
	caller: RootMgmntCaller
	model: string
}

class HttpError extends Error {
	status: number
	code: string
	params?: MessageParams

	constructor(
		status: number,
		code: string,
		params?: MessageParams,
		message?: string
	) {
		super(message ?? code)
		this.status = status
		this.code = code
		this.params = params
	}
}

function respondError(
	req: express.Request,
	res: express.Response,
	status: number,
	code: string,
	params?: MessageParams
): void {
	const locale = req.locale ?? resolveLocale(req)
	const message = translate(locale, `errors.${code}`, params)
	res.status(status).json({ error: code, code, message })
}

function handleError(
	req: express.Request,
	res: express.Response,
	error: unknown,
	fallback?: { status?: number; code?: string; params?: MessageParams }
): void {
	if (error instanceof HttpError) {
		respondError(req, res, error.status, error.code, error.params)
		return
	}
	console.error('unexpected backend error', error)
	if (fallback) {
		respondError(
			req,
			res,
			fallback.status ?? 500,
			fallback.code ?? 'internal_error',
			fallback.params
		)
	} else {
		respondError(req, res, 500, 'internal_error')
	}
}

declare global {
	namespace Express {
		interface Request {
			auth?: ClientIdentity
			ownerAuth?: OwnerRecord
			ownerAccessToken?: string
			locale?: Locale
		}
	}
}

function readPemFromEnvOrFile(valName: string, fileName: string): string {
	const v = process.env[valName]
	if (v && v.includes('-----BEGIN')) {
		// поддержка варианта с \n в строке, если вдруг останется
		return v.replace(/\\n/g, '\n').trim() + '\n'
	}
	const f = process.env[fileName]
	if (f) {
		const text = readFileSync(resolve(f), 'utf8')
		return text.trim() + '\n'
	}
	throw new Error(
		`Environment variable ${valName} is required (or set ${fileName})`
	)
}

function normalizePem(value: string): string {
	return value.includes('\n') ? value.replace(/\n/g, '\n') : value
}

function requireEnv(name: string): string {
	const value = process.env[name]
	if (!value || value.trim() === '') {
		throw new Error(`Environment variable ${name} is required`)
	}
	return value
}

function rootTokenFromReq(req: express.Request): string {
	return String(req.header('X-Root-Token') ?? '').trim()
}

function requireRootToken(req: express.Request, res: express.Response): boolean {
	const token = rootTokenFromReq(req)
	if (!token || token !== ROOT_TOKEN) {
		respondError(req, res, 401, 'unauthorized')
		return false
	}
	return true
}

const HOST = process.env['HOST'] ?? '0.0.0.0'
const PORT = Number.parseInt(process.env['PORT'] ?? '3030', 10)
const ROOT_TOKEN = process.env['ROOT_TOKEN'] ?? 'dev-root-token'
const WEB_SESSION_JWT_SECRET =
	(String(process.env['WEB_SESSION_JWT_SECRET'] ?? '').trim() ||
		String(process.env['ROOT_TOKEN'] ?? '').trim() ||
		ROOT_TOKEN)
const CA_KEY_PEM = readPemFromEnvOrFile('CA_KEY_PEM', 'CA_KEY_PEM_FILE')
const CA_CERT_PEM = readPemFromEnvOrFile('CA_CERT_PEM', 'CA_CERT_PEM_FILE')
/* TODO
const TLS_KEY_PEM  = readPemFromEnvOrFile('TLS_KEY_PEM',  'TLS_KEY_PEM_FILE');
const TLS_CERT_PEM = readPemFromEnvOrFile('TLS_CERT_PEM', 'TLS_CERT_PEM_FILE');
*/
const TLS_KEY_PEM = normalizePem(process.env['TLS_KEY_PEM'] ?? CA_KEY_PEM)
const TLS_CERT_PEM = normalizePem(process.env['TLS_CERT_PEM'] ?? CA_CERT_PEM)
const FORGE_GIT_URL = requireEnv('FORGE_GIT_URL')
// WebAuthn RP ID must be a registrable domain suffix of the current origin.
// Using the apex domain keeps it valid across app subdomains (app., v1.app., etc).
const WEB_RP_ID = process.env['WEB_RP_ID'] ?? 'inimatic.com'
const WEB_ORIGIN = process.env['WEB_ORIGIN'] ?? 'https://app.inimatic.com'
const ROOT_HUB_CONTROL_REPORTS_HASH = 'root:hub_control_reports'
const ROOT_CORE_UPDATE_REPORTS_HASH = 'root:hub_core_update_reports'
const ROOT_CORE_UPDATE_RELEASES_HASH = 'root:hub_core_update_releases'
const ROOT_CORE_UPDATE_SUBNETS_HASH = 'root:hub_core_update_subnets'
const ROOT_LLM_RESPONSE_CACHE_PREFIX = 'root:llm:response:v1'
const OWNER_REGISTRATION_URL = `${WEB_ORIGIN.replace(/\/+$/, '')}/?mode=registration`
const CORE_UPDATE_GITHUB_WEBHOOK_SECRET = (process.env['GITHUB_WEBHOOK_SECRET'] || '').trim()
const CORE_UPDATE_GITHUB_BRANCH = (process.env['CORE_UPDATE_GITHUB_BRANCH'] || process.env['ADAOS_INIT_REV'] || 'rev2026').trim()
const CORE_UPDATE_GITHUB_REPO = (process.env['CORE_UPDATE_GITHUB_REPO'] || 'stipot-com/adaos').trim().toLowerCase()
const CORE_UPDATE_GITHUB_COUNTDOWN_SEC =
	Number.parseFloat(process.env['CORE_UPDATE_GITHUB_COUNTDOWN_SEC'] || '60') || 60
const ROOT_LLM_REQUEST_DEDUPE_TTL_S =
	Number.parseInt(process.env['ROOT_LLM_REQUEST_DEDUPE_TTL_S'] || '600', 10) || 600
const ROOT_MGMNT_TOKEN =
	(String(process.env['ROOT_MGMNT_TOKEN'] ?? '').trim() ||
		String(process.env['ROOT_TOKEN'] ?? '').trim() ||
		ROOT_TOKEN)
const ROOT_MGMNT_STATE_KEY = 'root:mgmnt:state:v1'
const ROOT_MGMNT_AUDIT_KEY = 'root:mgmnt:audit:v1'
const ROOT_MGMNT_AUDIT_LIMIT = 200
const ROOT_MGMNT_LLM_USAGE_DAY_PREFIX = 'root:mgmnt:llm:usage:day'
const ROOT_MGMNT_LLM_DENY_DAY_PREFIX = 'root:mgmnt:llm:deny:day'
const ROOT_MGMNT_LLM_LAST_HASH = 'root:mgmnt:llm:last:v1'
const ROOT_MGMNT_LLM_STATS_TTL_S = 45 * 24 * 60 * 60

function rootMgmntTokenFromReq(req: express.Request): string {
	return String(req.header('X-Root-Mgmnt-Token') ?? req.header('X-Root-Token') ?? '').trim()
}

function requireRootMgmntToken(req: express.Request, res: express.Response): boolean {
	const token = rootMgmntTokenFromReq(req)
	if (!token || (token !== ROOT_MGMNT_TOKEN && token !== ROOT_TOKEN)) {
		respondError(req, res, 401, 'unauthorized')
		return false
	}
	return true
}

function nowIso(): string {
	return new Date().toISOString()
}

function normalizeStringArray(value: unknown): string[] {
	if (!Array.isArray(value)) return []
	return Array.from(
		new Set(
			value
				.map((item) => String(item ?? '').trim())
				.filter(Boolean),
		),
	).sort()
}

function defaultRootMgmntState(): RootMgmntState {
	const now = nowIso()
	return {
		version: 1,
		updated_at: now,
		policy: {
			llm_enabled: true,
			access_mode: 'open',
			default_model: (process.env['OPENAI_RESPONSES_MODEL'] ?? 'gpt-4o-mini').trim() || 'gpt-4o-mini',
			allowed_models: [],
			allowed_subnets: [],
			updated_at: now,
			updated_by: 'system.default',
		},
		subnets: {},
	}
}

function sanitizeRootMgmntState(raw: unknown): RootMgmntState {
	const fallback = defaultRootMgmntState()
	const payload = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
	const rawPolicy =
		payload['policy'] && typeof payload['policy'] === 'object'
			? (payload['policy'] as Record<string, unknown>)
			: {}
	const rawSubnets =
		payload['subnets'] && typeof payload['subnets'] === 'object'
			? (payload['subnets'] as Record<string, unknown>)
			: {}
	const subnets: Record<string, RootMgmntSubnetOverride> = {}
	for (const [subnetId, value] of Object.entries(rawSubnets)) {
		if (!subnetId) continue
		if (!value || typeof value !== 'object') continue
		const item = value as Record<string, unknown>
		const lifecycle =
			typeof item['lifecycle_state'] === 'string'
				? (String(item['lifecycle_state']).trim() as RootMgmntLifecycleState)
				: undefined
		const llmAccess =
			typeof item['llm_access'] === 'string'
				? (String(item['llm_access']).trim() as RootMgmntLlmAccess)
				: undefined
		subnets[subnetId] = {
			lifecycle_state: lifecycle,
			llm_access: llmAccess,
			note: typeof item['note'] === 'string' ? String(item['note']) : undefined,
			archived_at: typeof item['archived_at'] === 'string' ? String(item['archived_at']) : undefined,
			updated_at: typeof item['updated_at'] === 'string' ? String(item['updated_at']) : fallback.updated_at,
			updated_by: typeof item['updated_by'] === 'string' ? String(item['updated_by']) : 'system.default',
		}
	}
	return {
		version: 1,
		updated_at: typeof payload['updated_at'] === 'string' ? String(payload['updated_at']) : fallback.updated_at,
		policy: {
			llm_enabled:
				typeof rawPolicy['llm_enabled'] === 'boolean'
					? Boolean(rawPolicy['llm_enabled'])
					: fallback.policy.llm_enabled,
			access_mode:
				rawPolicy['access_mode'] === 'allowlist' || rawPolicy['access_mode'] === 'denyall'
					? (rawPolicy['access_mode'] as RootMgmntAccessMode)
					: 'open',
			default_model:
				typeof rawPolicy['default_model'] === 'string' && String(rawPolicy['default_model']).trim()
					? String(rawPolicy['default_model']).trim()
					: fallback.policy.default_model,
			allowed_models: normalizeStringArray(rawPolicy['allowed_models']),
			allowed_subnets: normalizeStringArray(rawPolicy['allowed_subnets']),
			updated_at:
				typeof rawPolicy['updated_at'] === 'string' ? String(rawPolicy['updated_at']) : fallback.policy.updated_at,
			updated_by:
				typeof rawPolicy['updated_by'] === 'string' ? String(rawPolicy['updated_by']) : fallback.policy.updated_by,
		},
		subnets,
	}
}

async function loadRootMgmntState(): Promise<RootMgmntState> {
	const raw = await redisClient.get(ROOT_MGMNT_STATE_KEY)
	if (!raw) return defaultRootMgmntState()
	try {
		return sanitizeRootMgmntState(JSON.parse(raw))
	} catch {
		return defaultRootMgmntState()
	}
}

async function saveRootMgmntState(state: RootMgmntState): Promise<RootMgmntState> {
	const now = nowIso()
	const normalized = sanitizeRootMgmntState({
		...state,
		updated_at: now,
		policy: {
			...state.policy,
			updated_at: typeof state.policy?.updated_at === 'string' ? state.policy.updated_at : now,
		},
	})
	await redisClient.set(ROOT_MGMNT_STATE_KEY, JSON.stringify(normalized))
	return normalized
}

async function appendRootMgmntAudit(event: Omit<RootMgmntAuditEvent, 'id' | 'created_at'>): Promise<RootMgmntAuditEvent> {
	const entry: RootMgmntAuditEvent = {
		id: uuidv4(),
		created_at: nowIso(),
		...event,
	}
	await redisClient.rPush(ROOT_MGMNT_AUDIT_KEY, JSON.stringify(entry))
	await redisClient.lTrim(ROOT_MGMNT_AUDIT_KEY, -ROOT_MGMNT_AUDIT_LIMIT, -1)
	return entry
}

async function readRootMgmntAudit(limit = 80): Promise<RootMgmntAuditEvent[]> {
	const count = Math.max(1, Math.min(200, Math.trunc(limit)))
	const raw = await redisClient.lRange(ROOT_MGMNT_AUDIT_KEY, -count, -1)
	return raw
		.map((item) => {
			try {
				const parsed = JSON.parse(item)
				return parsed && typeof parsed === 'object' ? (parsed as RootMgmntAuditEvent) : null
			} catch {
				return null
			}
		})
		.filter((item): item is RootMgmntAuditEvent => Boolean(item))
		.reverse()
}

function counterDayKey(ts = Date.now()): string {
	return new Date(ts).toISOString().slice(0, 10)
}

async function incrementRootMgmntCounter(prefix: string, field: string, amount = 1, ts = Date.now()): Promise<void> {
	if (!field) return
	const key = `${prefix}:${counterDayKey(ts)}`
	await redisClient.hIncrBy(key, field, Math.trunc(amount))
	await redisClient.expire(key, ROOT_MGMNT_LLM_STATS_TTL_S)
}

async function recordRootMgmntLlmEvent(options: {
	subnetId?: string | null
	model: string
	status: 'allowed' | 'denied'
}): Promise<void> {
	const subnetId = String(options.subnetId ?? '').trim()
	if (subnetId) {
		await incrementRootMgmntCounter(
			options.status === 'denied' ? ROOT_MGMNT_LLM_DENY_DAY_PREFIX : ROOT_MGMNT_LLM_USAGE_DAY_PREFIX,
			subnetId,
			1,
		)
		await redisClient.hSet(
			ROOT_MGMNT_LLM_LAST_HASH,
			subnetId,
			JSON.stringify({
				last_request_at: nowIso(),
				last_model: options.model,
				last_status: options.status,
			}),
		)
	}
}

async function aggregateRootMgmntCounter(prefix: string, days: number): Promise<Map<string, number>> {
	const limit = Math.max(1, Math.min(45, Math.trunc(days)))
	const totals = new Map<string, number>()
	for (let index = 0; index < limit; index += 1) {
		const ts = Date.now() - index * 24 * 60 * 60 * 1000
		const bucket = await redisClient.hGetAll(`${prefix}:${counterDayKey(ts)}`)
		for (const [field, rawValue] of Object.entries(bucket)) {
			const value = Number.parseInt(String(rawValue ?? '0'), 10)
			if (!Number.isFinite(value) || value <= 0) continue
			totals.set(field, (totals.get(field) ?? 0) + value)
		}
	}
	return totals
}

async function readRootMgmntLlmLastSeen(): Promise<Map<string, RootMgmntLlmLastSeen>> {
	const raw = await redisClient.hGetAll(ROOT_MGMNT_LLM_LAST_HASH)
	const out = new Map<string, RootMgmntLlmLastSeen>()
	for (const [subnetId, value] of Object.entries(raw)) {
		try {
			const parsed = JSON.parse(value)
			if (parsed && typeof parsed === 'object') {
				out.set(subnetId, parsed as RootMgmntLlmLastSeen)
			}
		} catch {
			// ignore malformed item
		}
	}
	return out
}

function numberFromUnknown(value: unknown): number | null {
	if (typeof value === 'number' && Number.isFinite(value)) {
		return value > 10_000_000_000 ? value : value * 1000
	}
	if (typeof value === 'string') {
		const trimmed = value.trim()
		if (!trimmed) return null
		const numeric = Number(trimmed)
		if (Number.isFinite(numeric)) {
			return numeric > 10_000_000_000 ? numeric : numeric * 1000
		}
		const parsed = Date.parse(trimmed)
		return Number.isFinite(parsed) ? parsed : null
	}
	return null
}

function maxTimestamp(...values: Array<number | null | undefined>): number | null {
	let max: number | null = null
	for (const value of values) {
		if (!Number.isFinite(value as number)) continue
		const current = Math.trunc(value as number)
		if (max === null || current > max) max = current
	}
	return max
}

function relativeAgeLabel(ts: number | null | undefined): string {
	if (!Number.isFinite(ts as number)) return 'never'
	const delta = Math.max(0, Date.now() - Number(ts))
	const hours = Math.floor(delta / (60 * 60 * 1000))
	if (hours < 1) return '<1h'
	if (hours < 24) return `${hours}h`
	const days = Math.floor(hours / 24)
	if (days < 30) return `${days}d`
	const months = Math.floor(days / 30)
	return `${months}mo`
}

function listChildDirectories(dirPath: string): string[] {
	try {
		if (!fs.existsSync(dirPath)) return []
		return fs
			.readdirSync(dirPath, { withFileTypes: true })
			.filter((entry) => entry.isDirectory())
			.map((entry) => entry.name)
			.sort()
	} catch {
		return []
	}
}

function readForgeStatsForSubnet(subnetId: string): RootMgmntForgeStats {
	const baseDir = path.join(forgeManagerWorkdir(), 'subnets', subnetId)
	const nodesRoot = path.join(baseDir, 'nodes')
	const nodeIds = listChildDirectories(nodesRoot)
	let skillDrafts = 0
	let scenarioDrafts = 0
	let uploads = 0
	for (const nodeId of nodeIds) {
		skillDrafts += listChildDirectories(path.join(nodesRoot, nodeId, 'skills')).length
		scenarioDrafts += listChildDirectories(path.join(nodesRoot, nodeId, 'scenarios')).length
		try {
			const uploadDir = path.join(nodesRoot, nodeId, 'uploads')
			if (fs.existsSync(uploadDir)) {
				uploads += fs
					.readdirSync(uploadDir, { withFileTypes: true })
					.filter((entry) => entry.isFile())
					.length
			}
		} catch {
			// ignore upload scan failures
		}
	}
	let skillRegistryVersions = 0
	let scenarioRegistryVersions = 0
	for (const itemName of listChildDirectories(path.join(baseDir, 'registry', 'skills'))) {
		skillRegistryVersions += listChildDirectories(path.join(baseDir, 'registry', 'skills', itemName)).length
	}
	for (const itemName of listChildDirectories(path.join(baseDir, 'registry', 'scenarios'))) {
		scenarioRegistryVersions += listChildDirectories(path.join(baseDir, 'registry', 'scenarios', itemName)).length
	}
	return {
		dev_nodes: nodeIds.length,
		uploads,
		draft_artifacts: skillDrafts + scenarioDrafts,
		registry_artifacts: skillRegistryVersions + scenarioRegistryVersions,
		skill_drafts: skillDrafts,
		scenario_drafts: scenarioDrafts,
		skill_registry_versions: skillRegistryVersions,
		scenario_registry_versions: scenarioRegistryVersions,
	}
}

function rootMgmntCallerFromReq(req: express.Request): RootMgmntCaller {
	const identity = getClientIdentity(req) ?? req.auth ?? null
	if (identity) {
		return {
			subnetId: identity.subnetId,
			nodeId: identity.type === 'node' ? identity.nodeId : null,
			source: 'mtls',
		}
	}
	const subnetHeader = String(req.header('X-AdaOS-Subnet-Id') ?? '').trim()
	const nodeHeader = String(req.header('X-AdaOS-Node-Id') ?? '').trim()
	if (subnetHeader) {
		return {
			subnetId: subnetHeader,
			nodeId: nodeHeader || null,
			source: 'header',
		}
	}
	return { subnetId: null, nodeId: null, source: 'unknown' }
}

function effectiveSubnetLlmAccess(
	override: RootMgmntSubnetOverride | undefined,
): RootMgmntLlmAccess {
	if (!override) return 'default'
	return override.llm_access === 'frozen' || override.llm_access === 'blocked'
		? override.llm_access
		: 'default'
}

async function evaluateRootLlmAccess(
	req: express.Request,
	options: { model: string; state?: RootMgmntState },
): Promise<RootMgmntLlmDecision> {
	const state = options.state ?? (await loadRootMgmntState())
	const caller = rootMgmntCallerFromReq(req)
	const subnetId = caller.subnetId
	const override = subnetId ? state.subnets[subnetId] : undefined
	const llmAccess = effectiveSubnetLlmAccess(override)
	if (!state.policy.llm_enabled) {
		return {
			allowed: false,
			status: 403,
			code: 'llm_disabled',
			reason: 'LLM access is disabled by root policy',
			state,
			caller,
			model: options.model,
		}
	}
	if (state.policy.access_mode === 'denyall') {
		return {
			allowed: false,
			status: 403,
			code: 'llm_denyall',
			reason: 'LLM access is in deny-all mode',
			state,
			caller,
			model: options.model,
		}
	}
	if (llmAccess === 'frozen' || llmAccess === 'blocked') {
		return {
			allowed: false,
			status: 403,
			code: llmAccess === 'blocked' ? 'subnet_blocked' : 'subnet_frozen',
			reason: `Subnet ${subnetId} is ${llmAccess}`,
			state,
			caller,
			model: options.model,
		}
	}
	if (override?.lifecycle_state === 'retired') {
		return {
			allowed: false,
			status: 403,
			code: 'subnet_retired',
			reason: `Subnet ${subnetId} is retired`,
			state,
			caller,
			model: options.model,
		}
	}
	if (state.policy.access_mode === 'allowlist') {
		if (!subnetId) {
			return {
				allowed: false,
				status: 401,
				code: 'subnet_identity_required',
				reason: 'Subnet identity is required in allowlist mode',
				state,
				caller,
				model: options.model,
			}
		}
		if (!state.policy.allowed_subnets.includes(subnetId)) {
			return {
				allowed: false,
				status: 403,
				code: 'subnet_not_allowed',
				reason: `Subnet ${subnetId} is not in the LLM allowlist`,
				state,
				caller,
				model: options.model,
			}
		}
	}
	if (state.policy.allowed_models.length && !state.policy.allowed_models.includes(options.model)) {
		return {
			allowed: false,
			status: 403,
			code: 'model_not_allowed',
			reason: `Model ${options.model} is not in the allowlist`,
			state,
			caller,
			model: options.model,
		}
	}
	return {
		allowed: true,
		status: 200,
		state,
		caller,
		model: options.model,
	}
}

async function rejectRootLlmRequest(
	req: express.Request,
	res: express.Response,
	decision: RootMgmntLlmDecision,
): Promise<void> {
	await recordRootMgmntLlmEvent({
		subnetId: decision.caller.subnetId,
		model: decision.model,
		status: 'denied',
	})
	await appendRootMgmntAudit({
		kind: 'llm_denied',
		action: 'llm_request',
		subnet_id: decision.caller.subnetId,
		node_id: decision.caller.nodeId,
		actor: `runtime.${decision.caller.source}`,
		status: 'denied',
		summary: decision.code ?? 'llm_denied',
		detail: {
			model: decision.model,
			reason: decision.reason ?? '',
		},
	})
	res.status(decision.status).json({
		ok: false,
		error: decision.code ?? 'llm_denied',
		reason: decision.reason ?? 'request denied by root policy',
		subnet_id: decision.caller.subnetId,
		model: decision.model,
	})
}

// Capture Root stdout/stderr for on-demand debugging via /v1/dev/logs.
try {
	installRootLogCapture({
		maxLines:
			Number.parseInt(String(process.env['ROOT_LOG_MAX_LINES'] || ''), 10) ||
			50_000,
	})
} catch {
	// best-effort
}

function resolveNodeYamlPath(): string | null {
	const explicit = (process.env['ADAOS_NODE_YAML_PATH'] || process.env['ADAOS_NODE_YAML'] || '').trim()
	if (explicit) return explicit
	// Try a few common dev layouts (repo root / container root).
	const candidates = [
		resolve(process.cwd(), '.adaos', 'node.yaml'),
		resolve(process.cwd(), '..', '.adaos', 'node.yaml'),
		resolve(process.cwd(), '..', '..', '.adaos', 'node.yaml'),
		resolve(process.cwd(), '..', '..', '..', '.adaos', 'node.yaml'),
		resolve(process.cwd(), '..', '..', '..', '..', '.adaos', 'node.yaml'),
	]
	for (const p of candidates) {
		try {
			if (fs.existsSync(p)) return p
		} catch {}
	}
	return null
}

const ROOT_SERVER_PROTO = (process.env['ROOT_SERVER_PROTO'] || process.env['SERVER_PROTO'] || 'https').toLowerCase()
const USE_HTTP_SERVER = ROOT_SERVER_PROTO === 'http'
if (USE_HTTP_SERVER) {
	console.warn('[root] starting in HTTP mode (ROOT_SERVER_PROTO=http)')
}

function resolveWebSessionTtlSeconds(): number {
	// Highest priority: explicit env override
	const envRaw = (process.env['WEB_SESSION_TTL_SECONDS'] || '').trim()
	if (envRaw) {
		const envVal = Number.parseInt(envRaw, 10)
		if (Number.isFinite(envVal) && envVal > 0) return envVal
	}
	// Optional: node.yaml override (dev/self-hosted hubs)
	const nodePath = resolveNodeYamlPath()
	if (nodePath) {
		try {
			const raw = readFileSync(nodePath, 'utf8')
			const cfg: any = YAML.parse(raw) || {}
			const ttl = cfg?.auth?.web_session_ttl_seconds?.owner ?? cfg?.auth?.web_session_ttl_seconds?.default
			const val = Number.parseInt(String(ttl ?? ''), 10)
			if (Number.isFinite(val) && val > 0) return val
		} catch {}
	}
	// Default: 1 hour
	return 3600
}

const WEB_SESSION_TTL_SECONDS = resolveWebSessionTtlSeconds()
const FORGE_SSH_KEY = process.env['FORGE_SSH_KEY']
const FORGE_AUTHOR_NAME = process.env['FORGE_GIT_AUTHOR_NAME'] ?? 'AdaOS Root'
const FORGE_AUTHOR_EMAIL =
	process.env['FORGE_GIT_AUTHOR_EMAIL'] ?? 'root@inimatic.local'
const FORGE_WORKDIR = process.env['FORGE_WORKDIR']
const SKILL_FORGE_KEY_PREFIX = 'forge:skills'
const SCENARIO_FORGE_KEY_PREFIX = 'forge:scenarios'
const BOOTSTRAP_TOKEN_TTL_SECONDS = 600
const HUB_FINGERPRINT_HASH = 'root:hub_fingerprints'

const policy = getPolicy()
const MAX_ARCHIVE_BYTES = policy.max_archive_mb * 1024 * 1024

const app = express()
let wsNatsProxyReady = false
let hubRouteProxyReady = false

const allowedCorsOrigins = new Set<string>()
const allowedCorsHosts = new Set<string>(['localhost', '127.0.0.1', '[::1]'])

function addAllowedCorsOrigin(candidate?: string | null) {
	if (!candidate) {
		return
	}
	const trimmed = candidate.trim()
	if (!trimmed) {
		return
	}
	try {
		const parsed = new URL(trimmed)
		const normalizedOrigin = parsed.origin
		if (normalizedOrigin) {
			allowedCorsOrigins.add(normalizedOrigin)
		}
		if (parsed.hostname) {
			allowedCorsHosts.add(parsed.hostname)
		}
	} catch {
		allowedCorsHosts.add(trimmed)
	}
}

addAllowedCorsOrigin(WEB_ORIGIN)
addAllowedCorsOrigin('https://app.inimatic.com')
addAllowedCorsOrigin('https://v1.app.inimatic.com')

const extraCorsOrigins =
	process.env['CORS_EXTRA_ORIGINS'] ?? process.env['CORS_ALLOWED_ORIGINS']
if (extraCorsOrigins) {
	for (const candidate of extraCorsOrigins.split(',')) {
		addAllowedCorsOrigin(candidate)
	}
}

function isCorsOriginAllowed(origin: string): boolean {
	if (allowedCorsOrigins.has(origin)) {
		return true
	}
	try {
		const parsed = new URL(origin)
		return (
			allowedCorsOrigins.has(parsed.origin) ||
			(parsed.hostname ? allowedCorsHosts.has(parsed.hostname) : false)
		)
	} catch {
		return allowedCorsHosts.has(origin)
	}
}

const corsOptions: CorsOptions = {
	origin(
		origin: string | undefined,
		callback: (err: Error | null, allow?: boolean) => void
	) {
		if (!origin || isCorsOriginAllowed(origin)) {
			callback(null, true)
			return
		}
		callback(new Error(`Origin ${origin} not allowed by CORS`))
	},
	methods: '*',
	allowedHeaders: '*',
	credentials: true,
	optionsSuccessStatus: 200,
}

app.use(cors(corsOptions))

function verifyGithubSignature(rawBody: Buffer, signatureHeader: string, secret: string): boolean {
	if (!secret || !signatureHeader) return false
	const expected = `sha256=${createHmac('sha256', secret).update(rawBody).digest('hex')}`
	try {
		return timingSafeEqual(Buffer.from(expected), Buffer.from(signatureHeader))
	} catch {
		return false
	}
}

function normalizeCoreUpdateSubnetState(record: Record<string, any>): Record<string, any> {
	const slotStatus = (record?.slot_status && typeof record.slot_status === 'object' ? record.slot_status : {}) as Record<string, any>
	const activeSlot = String(slotStatus?.active_slot || '').trim()
	const slots = (slotStatus?.slots && typeof slotStatus.slots === 'object' ? slotStatus.slots : {}) as Record<string, any>
	const activeManifest =
		activeSlot && slots[activeSlot] && typeof slots[activeSlot] === 'object' ? (slots[activeSlot].manifest as Record<string, any> | null) : null
	const status = (record?.status && typeof record.status === 'object' ? record.status : {}) as Record<string, any>
	const gitCommit = String(activeManifest?.git_commit || '').trim()
	const gitBranch = String(activeManifest?.git_branch || '').trim()
	const targetRev = String(activeManifest?.target_rev || status?.target_rev || '').trim()
	const currentBranch = gitBranch || targetRev
	const currentVersion = String(activeManifest?.target_version || status?.target_version || '').trim()
	return {
		subnet_id: String(record?.subnet_id || record?.hub_id || '').trim(),
		node_id: String(record?.node_id || '').trim(),
		role: String(record?.role || '').trim(),
		reported_at: Number(record?.reported_at || Date.now()),
		current_branch: currentBranch,
		current_commit: gitCommit,
		current_version: currentVersion,
		active_slot: activeSlot,
		previous_slot: String(slotStatus?.previous_slot || '').trim(),
		last_update_status: String(status?.state || '').trim(),
		last_update_message: String(status?.message || '').trim(),
		last_update_requested_rev: String(status?.target_rev || '').trim(),
		last_update_requested_version: String(status?.target_version || '').trim(),
		last_update_started_at: Number(status?.started_at || 0) || null,
		last_update_finished_at: Number(status?.finished_at || 0) || null,
		last_update_completed_at:
			String(status?.state || '').trim() === 'succeeded' ? Number(status?.finished_at || record?.reported_at || Date.now()) : null,
	}
}

function parseStoredHubReport(raw: string | null | undefined): Record<string, unknown> | null {
	if (!raw) return null
	try {
		const payload = JSON.parse(raw)
		return payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : null
	} catch {
		return null
	}
}

function protocolMetaFromRecord(record: Record<string, unknown> | null): {
	streamId: string
	messageId: string
	cursor: number | null
} {
	const protocol =
		record && typeof record['_protocol'] === 'object' && record['_protocol'] !== null
			? (record['_protocol'] as Record<string, unknown>)
			: null
	const streamId =
		typeof protocol?.['stream_id'] === 'string' ? String(protocol['stream_id']).trim() : ''
	const messageId =
		typeof protocol?.['message_id'] === 'string' ? String(protocol['message_id']).trim() : ''
	const cursorRaw = Number(protocol?.['cursor'])
	return {
		streamId,
		messageId,
		cursor: Number.isFinite(cursorRaw) ? Math.trunc(cursorRaw) : null,
	}
}

app.post('/v1/github/core_update/callback', express.raw({ type: 'application/json', limit: '2mb' }), async (req, res) => {
	if (!CORE_UPDATE_GITHUB_WEBHOOK_SECRET) {
		return res.status(404).json({ ok: false, error: 'github_webhook_disabled' })
	}
	const rawBody = Buffer.isBuffer(req.body) ? req.body : Buffer.from([])
	const signature = String(req.header('X-Hub-Signature-256') || '').trim()
	if (!verifyGithubSignature(rawBody, signature, CORE_UPDATE_GITHUB_WEBHOOK_SECRET)) {
		return res.status(401).json({ ok: false, error: 'invalid_signature' })
	}
	const eventName = String(req.header('X-GitHub-Event') || '').trim().toLowerCase()
	if (eventName === 'ping') {
		return res.json({ ok: true, event: 'ping' })
	}
	if (eventName !== 'push') {
		return res.json({ ok: true, ignored: true, reason: 'unsupported_event', event: eventName })
	}
	let body: any = {}
	try {
		body = JSON.parse(rawBody.toString('utf8') || '{}')
	} catch {
		return res.status(400).json({ ok: false, error: 'invalid_json' })
	}
	const ref = String(body?.ref || '').trim()
	const branch = ref.startsWith('refs/heads/') ? ref.slice('refs/heads/'.length) : ref
	const repoFullName = String(body?.repository?.full_name || '').trim().toLowerCase()
	if (CORE_UPDATE_GITHUB_REPO && repoFullName && repoFullName !== CORE_UPDATE_GITHUB_REPO) {
		return res.json({ ok: true, ignored: true, reason: 'repo_mismatch', repo: repoFullName })
	}
	if (!branch || branch !== CORE_UPDATE_GITHUB_BRANCH) {
		return res.json({ ok: true, ignored: true, reason: 'branch_mismatch', branch })
	}
	const connectedHubIds = listActiveHubIds()
	if (!connectedHubIds.length) {
		return res.json({ ok: true, branch, repo: repoFullName, hub_ids: [], results: [] })
	}
	const after = String(body?.after || '').trim()
	const payload = {
		target_rev: branch,
		target_version: after || '',
		reason: `github.push:${branch}${after ? `:${after.slice(0, 12)}` : ''}`,
		countdown_sec: CORE_UPDATE_GITHUB_COUNTDOWN_SEC,
		drain_timeout_sec: 10,
		signal_delay_sec: 0.25,
	}
	const releaseRecord = {
		branch,
		repo: repoFullName,
		head_sha: after,
		head_short_sha: after ? after.slice(0, 12) : '',
		published_at: Date.now(),
		countdown_sec: CORE_UPDATE_GITHUB_COUNTDOWN_SEC,
		reason: payload.reason,
	}
	await redisClient.hSet(ROOT_CORE_UPDATE_RELEASES_HASH, branch, JSON.stringify(releaseRecord))
	const rawSubnetStates = await redisClient.hGetAll(ROOT_CORE_UPDATE_SUBNETS_HASH)
	const subnet_ids = Object.keys(rawSubnetStates).sort()
	const results = await dispatchHubCoreUpdate('/api/admin/update/start', payload, connectedHubIds)
	return res.json({ ok: true, branch, repo: repoFullName, after, hub_ids: connectedHubIds, subnet_ids, results, release: releaseRecord })
})

app.use((req, _res, next) => {
	req.locale = resolveLocale(req)
	next()
})
app.use(express.json({ limit: '2mb' }))

// Public liveness probe for the Root backend itself.
// The frontend may hit this before a hub session is established (no hub_id yet).
app.get('/api/ping', (_req, res) => {
	res.status(200).json({ ok: true, service: 'root', ts: Date.now() })
})
app.get('/livez', (_req, res) => {
	res.status(200).json({ ok: true })
})

const EFFECTIVE_SOCKET_PATH = '/socket.io' as const
const SOCKET_CHANNEL_NS = '/adaos' as const
const SOCKET_CHANNEL_VERSION = 'v1' as const
const SOCKET_LEGACY_FALLBACK_ENABLED = false

const server = USE_HTTP_SERVER
	? http.createServer(app)
	: https.createServer(
			{
				key: TLS_KEY_PEM,
				cert: TLS_CERT_PEM,
				ca: [CA_CERT_PEM],
				requestCert: true,
				rejectUnauthorized: false,
			},
			app
		)

// Keep upgraded WS tunnels out of generic HTTP timeout logic.
// The `/nats`, `/hubs/*/ws`, and `/hubs/*/yws/*` paths can stay quiet for longer than the default
// HTTP server thresholds even when the underlying websocket is still healthy.
server.setTimeout(0)
server.requestTimeout = 0
server.headersTimeout = 0
server.keepAliveTimeout = 75_000

function tuneAcceptedSocket(socket: NetSocket | null | undefined): void {
	if (!socket) return
	try {
		socket.setTimeout(0)
	} catch {}
	try {
		socket.setKeepAlive(true, 20_000)
	} catch {}
	try {
		socket.setNoDelay(true)
	} catch {}
}

server.on('connection', (socket) => {
	tuneAcceptedSocket(socket)
})

const io = new Server(server, {
	cors: { origin: '*' },
	pingTimeout: 10000,
	pingInterval: 10000,
	path: EFFECTIVE_SOCKET_PATH,
})

type EngineAllowRequest = (
	req: IncomingMessage,
	callback: (err: string | null, success: boolean) => void
) => void

const engineWithAllowRequest = io.engine as typeof io.engine & {
	allowRequest?: EngineAllowRequest
}

function extractHandshakeToken(
	req: IncomingMessage,
	searchParams: URLSearchParams
): string | undefined {
	const authHeader = req.headers['authorization']
	if (Array.isArray(authHeader)) {
		for (const header of authHeader) {
			const token = extractBearer(header)
			if (token) {
				return token
			}
		}
	} else if (typeof authHeader === 'string') {
		const token = extractBearer(authHeader)
		if (token) {
			return token
		}
	}

	for (const candidate of [
		'token',
		'auth[token]',
		'authToken',
		'auth_token',
	]) {
		const value = searchParams.get(candidate)
		if (value && value.trim()) {
			return value.trim()
		}
	}

	for (const [key, value] of searchParams.entries()) {
		if (value && value.trim() && /token\]?$/i.test(key)) {
			return value.trim()
		}
	}

	return undefined
}

function extractBearer(headerValue: string): string | undefined {
	const trimmed = headerValue.trim()
	if (!trimmed) {
		return undefined
	}
	const match = trimmed.match(/^Bearer\s+(.+)$/i)
	return match ? match[1].trim() : undefined
}

engineWithAllowRequest.allowRequest = (req, callback) => {
	try {
		const url = new URL(req.url ?? '', 'http://localhost')
		const searchParams = url.searchParams

		if (searchParams.has('sid')) {
			callback(null, true)
			return
		}

		const namespace = searchParams.get('nsp') ?? '/'
		const token = extractHandshakeToken(req, searchParams)

		if (token) {
			callback(null, true)
			return
		}

		if (namespace === '/' && SOCKET_LEGACY_FALLBACK_ENABLED) {
			callback(null, true)
			return
		}

		callback('Unauthorized', false)
	} catch (error) {
		console.error('socket allowRequest error', error)
		callback('Unauthorized', false)
	}
	}

	installAdaosBridge(app, server)

	function resolveRedisUrl(): string {
		const explicit = (process.env['REDIS_URL'] || '').trim()
		if (explicit) return explicit
		const host =
			(process.env['REDIS_HOST'] || '').trim() ||
			(process.env['PRODUCTION'] ? 'redis' : 'localhost')
		const port = Number.parseInt((process.env['REDIS_PORT'] || '').trim() || '6379', 10) || 6379
		const db = (process.env['REDIS_DB'] || '').trim()
		return `redis://${host}:${port}${db ? `/${encodeURIComponent(db)}` : ''}`
	}

	const redisUrl = resolveRedisUrl()
	console.log(`[redis] connecting url=${redisUrl}`)
	const redisClient = await createClient({ url: redisUrl })
		.on('error', (err) => console.error('Redis Client Error', err))
		.connect()

const certificateAuthority = new CertificateAuthority({
	certPem: CA_CERT_PEM,
	keyPem: CA_KEY_PEM,
})
const forgeManager = new ForgeManager({
	repoUrl: FORGE_GIT_URL,
	workdir: FORGE_WORKDIR,
	authorName: FORGE_AUTHOR_NAME,
	authorEmail: FORGE_AUTHOR_EMAIL,
	sshKeyPath: FORGE_SSH_KEY,
})
await forgeManager.ensureReady()

// Устанавливаем WebAuthn эндпоинты (frontend ↔ root ↔ hub)
installWebAuthnRoutes(
	app,
	{
		redis: redisClient,
		defaultSessionTtlSeconds: WEB_SESSION_TTL_SECONDS,
		rpID: WEB_RP_ID,
		origin: WEB_ORIGIN,
		sessionJwtSecret: WEB_SESSION_JWT_SECRET,
	},
	respondError
)

const POLICY_RESPONSE = policy

const owners = new Map<string, OwnerRecord>()
const accessIndex = new Map<string, string>()
const refreshIndex = new Map<string, string>()
const deviceAuthorizations = new Map<string, DeviceAuthorization>()

function ownerHubSnapshot(): Map<string, { owner_id: string; last_seen_at: string; revoked: boolean }> {
	const result = new Map<string, { owner_id: string; last_seen_at: string; revoked: boolean }>()
	for (const owner of owners.values()) {
		for (const hub of owner.hubs.values()) {
			const existing = result.get(hub.hubId)
			const nextTs = hub.lastSeen.toISOString()
			if (!existing || Date.parse(existing.last_seen_at) < hub.lastSeen.getTime()) {
				result.set(hub.hubId, {
					owner_id: owner.ownerId,
					last_seen_at: nextTs,
					revoked: hub.revoked,
				})
			}
		}
	}
	return result
}

function activityScoreForSubnet(options: {
	liveNow: boolean
	lastSeenTs: number | null
	controlTs: number | null
	coreTs: number | null
	llmRequests7d: number
	llmRequests30d: number
	forge: RootMgmntForgeStats
}): number {
	let score = 0
	if (options.liveNow) score += 40
	const ageDays = Number.isFinite(options.lastSeenTs as number)
		? (Date.now() - Number(options.lastSeenTs)) / (24 * 60 * 60 * 1000)
		: Number.POSITIVE_INFINITY
	if (ageDays <= 1) score += 20
	else if (ageDays <= 7) score += 12
	else if (ageDays <= 30) score += 6
	const controlAgeDays = Number.isFinite(options.controlTs as number)
		? (Date.now() - Number(options.controlTs)) / (24 * 60 * 60 * 1000)
		: Number.POSITIVE_INFINITY
	if (controlAgeDays <= 1) score += 15
	else if (controlAgeDays <= 7) score += 8
	const coreAgeDays = Number.isFinite(options.coreTs as number)
		? (Date.now() - Number(options.coreTs)) / (24 * 60 * 60 * 1000)
		: Number.POSITIVE_INFINITY
	if (coreAgeDays <= 7) score += 6
	if (options.llmRequests7d >= 100) score += 14
	else if (options.llmRequests7d > 0) score += 8
	else if (options.llmRequests30d > 0) score += 4
	if (options.forge.dev_nodes > 0) score += 5
	if (options.forge.draft_artifacts > 0) score += 5
	if (options.forge.registry_artifacts > 0) score += 3
	return Math.max(0, Math.min(100, score))
}

function autoLifecycleStateForSubnet(options: {
	score: number
	liveNow: boolean
	lastSeenTs: number | null
	llmRequests30d: number
	forge: RootMgmntForgeStats
}): { state: RootMgmntLifecycleState; reason: string } {
	const ageDays = Number.isFinite(options.lastSeenTs as number)
		? (Date.now() - Number(options.lastSeenTs)) / (24 * 60 * 60 * 1000)
		: Number.POSITIVE_INFINITY
	if (!options.liveNow && ageDays >= 60 && options.llmRequests30d === 0) {
		return {
			state:
				options.forge.draft_artifacts > 0 || options.forge.registry_artifacts > 0 || options.forge.uploads > 0
					? 'retire_candidate'
					: 'dormant',
			reason: `idle ${Math.floor(ageDays)}d`,
		}
	}
	if (options.score >= 55) return { state: 'active', reason: 'recent activity' }
	if (options.score >= 30) return { state: 'warm', reason: 'intermittent activity' }
	if (options.score >= 15) return { state: 'stale', reason: 'old activity only' }
	return { state: 'dormant', reason: `low score ${options.score}` }
}

async function archiveSubnetDevSpace(subnetId: string): Promise<Record<string, unknown>> {
	const baseDir = path.join(forgeManagerWorkdir(), 'subnets', subnetId)
	if (!fs.existsSync(baseDir)) {
		return {
			ok: true,
			subnet_id: subnetId,
			archived: false,
			reason: 'forge_subnet_not_found',
			deleted: {
				drafts: 0,
				registry: 0,
			},
		}
	}

	const deletedDrafts: Array<Record<string, unknown>> = []
	const deletedRegistry: Array<Record<string, unknown>> = []

	for (const kind of ['skills', 'scenarios'] as DraftKind[]) {
		const names = new Set<string>()
		const nodesRoot = path.join(baseDir, 'nodes')
		for (const nodeId of listChildDirectories(nodesRoot)) {
			for (const name of listChildDirectories(path.join(nodesRoot, nodeId, kind))) {
				names.add(name)
			}
		}
		for (const name of Array.from(names).sort()) {
			try {
				const result = await forgeManager.deleteDraft({
					kind,
					subnetId,
					name,
					allNodes: true,
				})
				deletedDrafts.push({
					kind,
					name,
					deleted: result.deleted.length,
					audit_id: result.auditId,
				})
				if (result.redisKeys?.length) {
					await redisClient.del(result.redisKeys)
				}
			} catch (error: any) {
				if (error?.code !== 'not_found') throw error
			}
		}
	}

	for (const kind of ['skills', 'scenarios'] as DraftKind[]) {
		const registryRoot = path.join(baseDir, 'registry', kind)
		for (const name of listChildDirectories(registryRoot)) {
			try {
				const result = await forgeManager.deleteRegistry({
					kind,
					subnetId,
					name,
					allVersions: true,
					force: true,
				})
				deletedRegistry.push({
					kind,
					name,
					deleted: result.deleted.length,
					tombstoned: Boolean(result.tombstoned),
					audit_id: result.auditId,
				})
			} catch (error: any) {
				if (error?.code !== 'not_found') throw error
			}
		}
	}

	return {
		ok: true,
		subnet_id: subnetId,
		archived: deletedDrafts.length > 0 || deletedRegistry.length > 0,
		deleted: {
			drafts: deletedDrafts.length,
			registry: deletedRegistry.length,
			draft_items: deletedDrafts,
			registry_items: deletedRegistry,
		},
	}
}

async function applyRootMgmntSubnetAction(options: {
	subnetId: string
	action: 'freeze_llm' | 'unfreeze_llm' | 'mark_dormant' | 'reactivate' | 'archive_dev_space' | 'retire_subnet'
	actor: string
	note?: string
}): Promise<Record<string, unknown>> {
	const subnetId = String(options.subnetId || '').trim()
	if (!subnetId) {
		throw new HttpError(400, 'invalid_request', undefined, 'subnet_id is required')
	}
	const state = await loadRootMgmntState()
	const nextState = sanitizeRootMgmntState(state)
	const now = nowIso()
	const current = { ...(nextState.subnets[subnetId] ?? { updated_at: now, updated_by: options.actor }) }
	let archiveResult: Record<string, unknown> | null = null

	if (options.action === 'freeze_llm') {
		current.llm_access = 'frozen'
	}
	if (options.action === 'unfreeze_llm') {
		delete current.llm_access
	}
	if (options.action === 'mark_dormant') {
		current.lifecycle_state = 'dormant'
	}
	if (options.action === 'reactivate') {
		delete current.lifecycle_state
		delete current.llm_access
		delete current.archived_at
	}
	if (options.action === 'archive_dev_space') {
		archiveResult = await archiveSubnetDevSpace(subnetId)
		current.archived_at = now
	}
	if (options.action === 'retire_subnet') {
		archiveResult = await archiveSubnetDevSpace(subnetId)
		current.lifecycle_state = 'retired'
		current.llm_access = 'blocked'
		current.archived_at = now
	}

	current.updated_at = now
	current.updated_by = options.actor
	if (options.note) current.note = options.note
	nextState.subnets[subnetId] = current
	await saveRootMgmntState(nextState)
	await appendRootMgmntAudit({
		kind: 'subnet_action',
		action: options.action,
		subnet_id: subnetId,
		actor: options.actor,
		status: 'ok',
		summary: `${options.action}:${subnetId}`,
		detail: archiveResult ? { archive: archiveResult } : undefined,
	})
	return {
		ok: true,
		subnet_id: subnetId,
		action: options.action,
		override: current,
		archive: archiveResult,
	}
}

async function updateRootMgmntPolicy(options: {
	actor: string
	llm_enabled?: boolean
	access_mode?: RootMgmntAccessMode
	default_model?: string
	allowed_models?: string[]
	allowed_subnets?: string[]
}): Promise<RootMgmntPolicyState> {
	const state = await loadRootMgmntState()
	const nextState = sanitizeRootMgmntState(state)
	const nextPolicy: RootMgmntPolicyState = {
		...nextState.policy,
		updated_at: nowIso(),
		updated_by: options.actor,
	}
	if (typeof options.llm_enabled === 'boolean') {
		nextPolicy.llm_enabled = options.llm_enabled
	}
	if (options.access_mode) {
		nextPolicy.access_mode = options.access_mode
	}
	if (typeof options.default_model === 'string' && options.default_model.trim()) {
		nextPolicy.default_model = options.default_model.trim()
	}
	if (options.allowed_models) {
		nextPolicy.allowed_models = normalizeStringArray(options.allowed_models)
	}
	if (options.allowed_subnets) {
		nextPolicy.allowed_subnets = normalizeStringArray(options.allowed_subnets)
	}
	nextState.policy = nextPolicy
	await saveRootMgmntState(nextState)
	await appendRootMgmntAudit({
		kind: 'policy_update',
		action: 'update_policy',
		actor: options.actor,
		status: 'ok',
		summary: `mode=${nextPolicy.access_mode}, enabled=${nextPolicy.llm_enabled}`,
		detail: {
			default_model: nextPolicy.default_model,
			allowed_models: nextPolicy.allowed_models,
			allowed_subnets: nextPolicy.allowed_subnets,
		},
	})
	return nextPolicy
}

async function buildRootMgmntSnapshot(): Promise<Record<string, unknown>> {
	const state = await loadRootMgmntState()
	const registeredRaw = await redisClient.hGetAll('root:subnets')
	const controlRaw = await redisClient.hGetAll(ROOT_HUB_CONTROL_REPORTS_HASH)
	const coreRaw = await redisClient.hGetAll(ROOT_CORE_UPDATE_REPORTS_HASH)
	const llmUsage24h = await aggregateRootMgmntCounter(ROOT_MGMNT_LLM_USAGE_DAY_PREFIX, 1)
	const llmUsage7d = await aggregateRootMgmntCounter(ROOT_MGMNT_LLM_USAGE_DAY_PREFIX, 7)
	const llmUsage30d = await aggregateRootMgmntCounter(ROOT_MGMNT_LLM_USAGE_DAY_PREFIX, 30)
	const llmDenied30d = await aggregateRootMgmntCounter(ROOT_MGMNT_LLM_DENY_DAY_PREFIX, 30)
	const llmLastSeen = await readRootMgmntLlmLastSeen()
	const ownerSnapshot = ownerHubSnapshot()
	const connectedHubIds = new Set(listActiveHubIds())
	const subnetIds = new Set<string>()

	for (const subnetId of Object.keys(registeredRaw)) subnetIds.add(subnetId)
	for (const subnetId of Object.keys(controlRaw)) subnetIds.add(subnetId)
	for (const subnetId of Object.keys(coreRaw)) subnetIds.add(subnetId)
	for (const subnetId of ownerSnapshot.keys()) subnetIds.add(subnetId)
	for (const subnetId of connectedHubIds.values()) subnetIds.add(subnetId)
	for (const subnetId of Object.keys(state.subnets)) subnetIds.add(subnetId)

	const fleet: Array<Record<string, unknown>> = []
	let totalRequests24h = 0
	let totalDenied30d = 0
	let totalDormant = 0
	let totalRetireCandidates = 0

	for (const subnetId of Array.from(subnetIds).sort()) {
		const registered = parseStoredHubReport(registeredRaw[subnetId])
		const control = parseStoredHubReport(controlRaw[subnetId])
		const core = parseStoredHubReport(coreRaw[subnetId])
		const owner = ownerSnapshot.get(subnetId)
		const forge = readForgeStatsForSubnet(subnetId)
		const llmLast = llmLastSeen.get(subnetId)
		const llm24 = llmUsage24h.get(subnetId) ?? 0
		const llm7 = llmUsage7d.get(subnetId) ?? 0
		const llm30 = llmUsage30d.get(subnetId) ?? 0
		const deny30 = llmDenied30d.get(subnetId) ?? 0
		const registeredTs = numberFromUnknown(registered?.created_at)
		const ownerTs = numberFromUnknown(owner?.last_seen_at)
		const controlTs = maxTimestamp(
			numberFromUnknown(control?.root_received_at),
			numberFromUnknown(control?.reported_at),
		)
		const coreTs = maxTimestamp(
			numberFromUnknown(core?.root_received_at),
			numberFromUnknown(core?.reported_at),
			numberFromUnknown((core?.status as Record<string, unknown> | undefined)?.['finished_at']),
		)
		const llmTs = numberFromUnknown(llmLast?.last_request_at)
		const lastSeenTs = maxTimestamp(registeredTs, ownerTs, controlTs, coreTs, llmTs)
		const liveNow = connectedHubIds.has(subnetId)
		const score = activityScoreForSubnet({
			liveNow,
			lastSeenTs,
			controlTs,
			coreTs,
			llmRequests7d: llm7,
			llmRequests30d: llm30,
			forge,
		})
		const autoState = autoLifecycleStateForSubnet({
			score,
			liveNow,
			lastSeenTs,
			llmRequests30d: llm30,
			forge,
		})
		const override = state.subnets[subnetId]
		const lifecycleState = override?.lifecycle_state || autoState.state
		const llmAccess = effectiveSubnetLlmAccess(override)
		const retireCandidate =
			lifecycleState === 'retire_candidate' ||
			(autoState.state === 'retire_candidate' && lifecycleState !== 'retired')
		if (lifecycleState === 'dormant') totalDormant += 1
		if (retireCandidate) totalRetireCandidates += 1
		totalRequests24h += llm24
		totalDenied30d += deny30
		const ageDays = Number.isFinite(lastSeenTs as number)
			? Math.floor((Date.now() - Number(lastSeenTs)) / (24 * 60 * 60 * 1000))
			: null
		const reasonParts = [
			autoState.reason,
			llm30 === 0 ? 'no llm 30d' : '',
			forge.draft_artifacts > 0 ? `${forge.draft_artifacts} drafts` : '',
			forge.registry_artifacts > 0 ? `${forge.registry_artifacts} registry` : '',
			owner?.revoked ? 'owner revoked' : '',
		].filter(Boolean)

		fleet.push({
			subnet_id: subnetId,
			owner_id: owner?.owner_id ?? '',
			owner_revoked: Boolean(owner?.revoked),
			live_now: liveNow ? 'yes' : 'no',
			last_seen: relativeAgeLabel(lastSeenTs),
			last_seen_at: lastSeenTs ? new Date(lastSeenTs).toISOString() : '',
			idle_days: ageDays ?? '',
			activity_score: score,
			auto_state: autoState.state,
			lifecycle_state: lifecycleState,
			llm_access: llmAccess,
			llm_requests_24h: llm24,
			llm_requests_7d: llm7,
			llm_requests_30d: llm30,
			llm_denied_30d: deny30,
			llm_last_seen_at: llmLast?.last_request_at ?? '',
			llm_last_model: llmLast?.last_model ?? '',
			dev_nodes: forge.dev_nodes,
			draft_artifacts: forge.draft_artifacts,
			registry_artifacts: forge.registry_artifacts,
			uploads: forge.uploads,
			retire_candidate: retireCandidate,
			candidate_reason: reasonParts.join('; '),
			note: override?.note ?? '',
			can_freeze: llmAccess === 'default',
			can_unfreeze: llmAccess !== 'default',
			can_mark_dormant: lifecycleState !== 'dormant' && lifecycleState !== 'retired',
			can_reactivate: lifecycleState === 'dormant' || lifecycleState === 'retired' || llmAccess !== 'default',
			can_archive: forge.draft_artifacts + forge.registry_artifacts + forge.uploads > 0,
			can_retire: lifecycleState !== 'retired',
			control_status:
				typeof (control?.lifecycle as Record<string, unknown> | undefined)?.['node_state'] === 'string'
					? String((control?.lifecycle as Record<string, unknown>)['node_state'])
					: '',
			route_status:
				typeof (control?.route as Record<string, unknown> | undefined)?.['status'] === 'string'
					? String((control?.route as Record<string, unknown>)['status'])
					: '',
		})
	}

	const liveSubnets = fleet.filter((item) => item['live_now'] === 'yes').length
	const archiveCandidates = fleet.filter(
		(item) =>
			Number(item['draft_artifacts'] ?? 0) + Number(item['registry_artifacts'] ?? 0) + Number(item['uploads'] ?? 0) >
			0,
	).length
	const audit = await readRootMgmntAudit(80)
	const topCandidates = fleet
		.filter((item) => Boolean(item['retire_candidate']))
		.sort((left, right) => Number(right['idle_days'] ?? 0) - Number(left['idle_days'] ?? 0))
		.slice(0, 8)

	return {
		ok: true,
		generated_at: nowIso(),
		overview: {
			total_subnets: fleet.length,
			live_subnets: liveSubnets,
			dormant_subnets: totalDormant,
			retire_candidates: totalRetireCandidates,
			archive_candidates: archiveCandidates,
			llm_requests_24h: totalRequests24h,
			llm_denied_30d: totalDenied30d,
		},
		policy: state.policy,
		fleet,
		lifecycle_candidates: topCandidates,
		audit,
	}
}

function generateToken(prefix: string): string {
	return `${prefix}_${randomBytes(24).toString('hex')}`
}

function generateUserCode(): string {
	const raw = randomBytes(4).toString('hex').toUpperCase()
	return `${raw.slice(0, 4)}-${raw.slice(4)}`
}

function ensureOwnerRecord(ownerId: string): OwnerRecord {
	const existing = owners.get(ownerId)
	if (existing) {
		return existing
	}
	const record: OwnerRecord = {
		ownerId,
		subject: `owner:${ownerId}`,
		scopes: ['owner'],
		refreshToken: generateToken('rt'),
		accessToken: '',
		accessExpiresAt: new Date(0),
		hubs: new Map(),
		createdAt: new Date(),
		updatedAt: new Date(),
	}
	owners.set(ownerId, record)
	refreshIndex.set(record.refreshToken, ownerId)
	return record
}

function issueAccessToken(
	owner: OwnerRecord,
	scopes?: string[],
	subject?: string | null
): { token: string; expiresAt: Date } {
	if (owner.accessToken) {
		accessIndex.delete(owner.accessToken)
	}
	owner.updatedAt = new Date()
	if (scopes && scopes.length) {
		owner.scopes = scopes
	}
	if (typeof subject === 'string') {
		owner.subject = subject
	}
	const token = generateToken('at')
	const expiresAt = new Date(Date.now() + 60 * 60 * 1000)
	owner.accessToken = token
	owner.accessExpiresAt = expiresAt
	accessIndex.set(token, owner.ownerId)
	return { token, expiresAt }
}

function updateRefreshToken(owner: OwnerRecord, refreshToken?: string): string {
	if (refreshToken && refreshToken !== owner.refreshToken) {
		refreshIndex.delete(owner.refreshToken)
		owner.refreshToken = refreshToken
	}
	refreshIndex.set(owner.refreshToken, owner.ownerId)
	return owner.refreshToken
}

function authenticateOwnerBearer(
	req: express.Request,
	res: express.Response,
	next: express.NextFunction
) {
	const header = req.header('Authorization') ?? ''
	const token = header.startsWith('Bearer ')
		? header.slice('Bearer '.length).trim()
		: ''
	if (!token || !accessIndex.has(token)) {
		respondError(req, res, 401, 'invalid_token')
		return
	}
	const ownerId = accessIndex.get(token)!
	const owner = owners.get(ownerId)
	if (!owner) {
		respondError(req, res, 401, 'invalid_token')
		return
	}
	if (
		owner.accessToken !== token ||
		owner.accessExpiresAt.getTime() <= Date.now()
	) {
		respondError(req, res, 401, 'token_expired')
		return
	}
	req.ownerAuth = owner
	req.ownerAccessToken = token
	next()
}

function requireJsonField(body: unknown, field: string): string {
	if (!body || typeof body !== 'object') {
		throw new HttpError(400, 'missing_field', { field })
	}
	const value = (body as Record<string, unknown>)[field]
	if (typeof value !== 'string' || value.trim() === '') {
		throw new HttpError(400, 'missing_field', { field })
	}
	return value.trim()
}

function parseTtl(ttl: unknown): number | undefined {
	if (typeof ttl !== 'string' || ttl.trim() === '') {
		return undefined
	}
	const match = ttl.trim().match(/^([0-9]+)([smhd])$/i)
	if (!match) {
		return undefined
	}
	const value = Number.parseInt(match[1], 10)
	const unit = match[2].toLowerCase()
	const seconds =
		unit === 's'
			? value
			: unit === 'm'
				? value * 60
				: unit === 'h'
					? value * 60 * 60
					: value * 24 * 60 * 60
	return seconds / (24 * 60 * 60)
}

function generateSubnetId(): string {
	return `sn_${uuidv4().replace(/-/g, '').slice(0, 8)}`
}

function generateNodeId(): string {
	return `node_${uuidv4().replace(/-/g, '').slice(0, 8)}`
}

function assertSafeName(name: string): void {
	if (
		!name ||
		name.includes('..') ||
		name.includes('/') ||
		name.includes('\\')
	) {
		throw new HttpError(400, 'invalid_name')
	}
	if (path.basename(name) !== name) throw new HttpError(400, 'invalid_name')
}

function decodeArchive(archiveB64: string): Buffer {
	try {
		return Buffer.from(archiveB64, 'base64')
	} catch (error) {
		throw new HttpError(400, 'invalid_archive_encoding')
	}
}

function verifySha256(buffer: Buffer, expected?: string): void {
	if (!expected) {
		return
	}
	const normalized = expected.trim().toLowerCase()
	const actual = createHash('sha256')
		.update(buffer)
		.digest('hex')
		.toLowerCase()
	if (normalized !== actual) {
		throw new HttpError(400, 'sha256_mismatch')
	}
}

async function useBootstrapToken(
	token: string
): Promise<Record<string, unknown>> {
	const key = `bootstrap:${token}`
	const value = await redisClient.get(key)
	if (!value) {
		throw new HttpError(401, 'invalid_bootstrap_token')
	}
	await redisClient.del(key)
	try {
		return JSON.parse(value)
	} catch {
		return {}
	}
}

function getPeerCertificate(req: express.Request): PeerCertificate | null {
	const tlsSocket = req.socket as TLSSocket
	const certificate = tlsSocket.getPeerCertificate()
	if (!certificate || Object.keys(certificate).length === 0) {
		return null
	}
	return certificate
}

function parseClientIdentity(cert: PeerCertificate): ClientIdentity | null {
	const subject = cert.subject
	const subjectRecord = subject as unknown as
		| Partial<Record<string, string>>
		| undefined
	const cn = subjectRecord?.['CN'] ?? subjectRecord?.['cn']
	if (!cn) {
		console.warn('mTLS identity parse failed: missing common name', {
			subject: subjectRecord,
		})
		return null
	}
	if (cn.startsWith('subnet:')) {
		const subnetId = cn.slice('subnet:'.length)
		if (!subnetId) {
			console.warn(
				'mTLS identity parse failed: empty subnet common name',
				{ subject: subjectRecord }
			)
			return null
		}
		return { type: 'hub', subnetId }
	}
	if (cn.startsWith('node:')) {
		const nodeId = cn.slice('node:'.length)
		const org = subjectRecord?.['O'] ?? subjectRecord?.['o']
		if (!nodeId || !org || !org.startsWith('subnet:')) {
			console.warn(
				'mTLS identity parse failed: node subject missing subnet binding',
				{ subject: subjectRecord }
			)
			return null
		}
		const subnetId = org.slice('subnet:'.length)
		if (!subnetId) {
			console.warn(
				'mTLS identity parse failed: empty subnet organization',
				{ subject: subjectRecord }
			)
			return null
		}
		return { type: 'node', subnetId, nodeId }
	}
	console.warn('mTLS identity parse failed: unsupported common name', {
		subject: subjectRecord,
	})
	return null
}

function getClientIdentity(req: express.Request): ClientIdentity | null {
	const tlsSocket = req.socket as TLSSocket
	if (!tlsSocket.authorized) {
		return null
	}
	const cert = getPeerCertificate(req)
	if (!cert) {
		return null
	}
	return parseClientIdentity(cert)
}

app.get('/health', (_req, res) => {
	res.status(200).type('text/plain').send('ok')
})

function getReadinessPayload() {
	const routeProxyEnabled = Boolean(process.env['NATS_URL'])
	const ready = wsNatsProxyReady && (!routeProxyEnabled || hubRouteProxyReady)
	return {
		ok: ready,
		ready,
		version: buildInfo.version,
		build_date: buildInfo.buildDate,
		commit: buildInfo.commit,
		time: new Date().toISOString(),
		mtls: true,
		ws_nats_proxy_ready: wsNatsProxyReady,
		hub_route_proxy_ready: hubRouteProxyReady,
		hub_route_proxy_enabled: routeProxyEnabled,
	}
}

function sendReadiness(res: express.Response): void {
	const payload = getReadinessPayload()
	res.status(payload.ready ? 200 : 503).json(payload)
}

app.get('/healthz', (_req, res) => {
	sendReadiness(res)
})

app.get('/readyz', (_req, res) => {
	sendReadiness(res)
})

app.get('/v1/health', (_req, res) => {
	res.json({
		ok: true,
		version: buildInfo.version,
		build_date: buildInfo.buildDate,
		commit: buildInfo.commit,
		time: new Date().toISOString(),
	})
})

app.get('/v1/root_mgmnt/snapshot', async (req, res) => {
	if (!requireRootMgmntToken(req, res)) return
	try {
		return res.json(await buildRootMgmntSnapshot())
	} catch (error) {
		console.error('root_mgmnt snapshot failed', error)
		return handleError(req, res, error, {
			status: 500,
			code: 'internal_error',
		})
	}
})

app.post('/v1/root_mgmnt/policy', async (req, res) => {
	if (!requireRootMgmntToken(req, res)) return
	try {
		const body = typeof req.body === 'object' && req.body !== null ? req.body : {}
		const actor =
			(typeof req.header('X-Root-Mgmnt-Actor') === 'string' && String(req.header('X-Root-Mgmnt-Actor')).trim()) ||
			(typeof (body as any).actor === 'string' && String((body as any).actor).trim()) ||
			'root_mgmnt'
		const policy = await updateRootMgmntPolicy({
			actor,
			llm_enabled:
				typeof (body as any).llm_enabled === 'boolean' ? Boolean((body as any).llm_enabled) : undefined,
			access_mode:
				(body as any).access_mode === 'allowlist' || (body as any).access_mode === 'denyall'
					? ((body as any).access_mode as RootMgmntAccessMode)
					: (body as any).access_mode === 'open'
						? 'open'
						: undefined,
			default_model:
				typeof (body as any).default_model === 'string' ? String((body as any).default_model) : undefined,
			allowed_models: Array.isArray((body as any).allowed_models) ? (body as any).allowed_models : undefined,
			allowed_subnets: Array.isArray((body as any).allowed_subnets) ? (body as any).allowed_subnets : undefined,
		})
		return res.json({ ok: true, policy })
	} catch (error) {
		return handleError(req, res, error, {
			status: 400,
			code: 'invalid_request',
		})
	}
})

app.post('/v1/root_mgmnt/subnets/:subnetId/action', async (req, res) => {
	if (!requireRootMgmntToken(req, res)) return
	try {
		const subnetId = String(req.params['subnetId'] || '').trim()
		const body = typeof req.body === 'object' && req.body !== null ? req.body : {}
		const action = String((body as any).action || '').trim()
		if (
			![
				'freeze_llm',
				'unfreeze_llm',
				'mark_dormant',
				'reactivate',
				'archive_dev_space',
				'retire_subnet',
			].includes(action)
		) {
			return respondError(req, res, 400, 'invalid_request')
		}
		const actor =
			(typeof req.header('X-Root-Mgmnt-Actor') === 'string' && String(req.header('X-Root-Mgmnt-Actor')).trim()) ||
			(typeof (body as any).actor === 'string' && String((body as any).actor).trim()) ||
			'root_mgmnt'
		const note = typeof (body as any).note === 'string' ? String((body as any).note).trim() : undefined
		return res.json(
			await applyRootMgmntSubnetAction({
				subnetId,
				action: action as
					| 'freeze_llm'
					| 'unfreeze_llm'
					| 'mark_dormant'
					| 'reactivate'
					| 'archive_dev_space'
					| 'retire_subnet',
				actor,
				note,
			}),
		)
	} catch (error) {
		return handleError(req, res, error, {
			status: 400,
			code: 'invalid_request',
		})
	}
})

app.get('/v1/llm/models', async (req, res) => {
	const apiKey = (process.env['OPENAI_API_KEY'] ?? '').trim()
	if (!apiKey) {
		return res.status(503).json({ ok: false, error: 'openai_api_key_missing' })
	}

	try {
		const state = await loadRootMgmntState()
		const model = state.policy.default_model || (process.env['OPENAI_RESPONSES_MODEL'] ?? 'gpt-4o-mini')
		const decision = await evaluateRootLlmAccess(req, { model, state })
		if (!decision.allowed) {
			return await rejectRootLlmRequest(req, res, decision)
		}
		const r = await fetch('https://api.openai.com/v1/models', {
			method: 'GET',
			headers: {
				authorization: `Bearer ${apiKey}`,
			},
		})

		const text = await r.text()
		let data: any = null
		if (text) {
			try {
				data = JSON.parse(text)
			} catch (e: any) {
				console.warn('llm models upstream returned non-JSON payload', e)
			}
		}

		if (!r.ok) {
			return res
				.status(r.status)
				.json(data ?? { ok: false, error: 'llm_models_upstream_failed', status: r.status })
		}

		if (Array.isArray(data?.data) && decision.state.policy.allowed_models.length) {
			const allow = new Set(decision.state.policy.allowed_models)
			data = {
				...data,
				data: data.data.filter((item: any) => allow.has(String(item?.id ?? ''))),
			}
		}

		return res.json(data ?? { ok: true })
	} catch (error: any) {
		console.error('llm models proxy failed', error)
		return res
			.status(502)
			.json({ ok: false, error: 'llm_models_proxy_failed', detail: String(error?.message ?? error) })
	}
})

function llmRequestId(body: Record<string, unknown>): string {
	const raw = body['request_id']
	return typeof raw === 'string' ? raw.trim() : ''
}

function llmRequestFingerprint(payload: Record<string, unknown>): string {
	return createHash('sha256')
		.update(JSON.stringify(payload))
		.digest('hex')
}

function llmCacheKey(requestId: string): string {
	return `${ROOT_LLM_RESPONSE_CACHE_PREFIX}:${requestId}`
}

function attachLlmProtocolMeta(payload: unknown, meta: Record<string, unknown>): Record<string, unknown> {
	if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
		return { ...(payload as Record<string, unknown>), _protocol: meta }
	}
	return { ok: true, value: payload, _protocol: meta }
}

app.post('/v1/llm/response', async (req, res) => {
	const apiKey = (process.env['OPENAI_API_KEY'] ?? '').trim()
	if (!apiKey) {
		return res.status(503).json({ ok: false, error: 'openai_api_key_missing' })
	}

	try {
		const state = await loadRootMgmntState()
		const body = req.body ?? {}
		const model =
			(typeof body.model === 'string' && body.model.trim()) ||
			state.policy.default_model ||
			(process.env['OPENAI_RESPONSES_MODEL'] ?? 'gpt-4o-mini')
		const decision = await evaluateRootLlmAccess(req, { model, state })
		if (!decision.allowed) {
			return await rejectRootLlmRequest(req, res, decision)
		}
		const messages = Array.isArray(body.messages) ? body.messages : []
		const requestId = llmRequestId(body)

		const input = messages.map((m: any) => ({
			role: typeof m?.role === 'string' ? m.role : 'user',
			content: [
				{
					type: 'input_text',
					text: typeof m?.content === 'string' ? m.content : '',
				},
			],
		}))

		const openaiPayload: any = { model, input }
		if (typeof body.temperature === 'number') {
			openaiPayload.temperature = body.temperature
		}
		if (typeof body.max_tokens === 'number') {
			openaiPayload.max_output_tokens = body.max_tokens
		}
		if (typeof body.top_p === 'number') {
			openaiPayload.top_p = body.top_p
		}
		const fingerprint = requestId ? llmRequestFingerprint(openaiPayload) : ''
		if (requestId) {
			const cachedRaw = await redisClient.get(llmCacheKey(requestId))
			if (cachedRaw) {
				try {
					const cached = JSON.parse(cachedRaw)
					if (cached && typeof cached === 'object') {
						const cachedFingerprint =
							typeof cached['request_fingerprint'] === 'string' ? cached['request_fingerprint'] : ''
						if (cachedFingerprint && cachedFingerprint !== fingerprint) {
							return res.status(409).json({
								ok: false,
								error: 'llm_request_id_conflict',
								request_id: requestId,
							})
						}
						const cachedResponse = cached['response']
						return res.json(
							attachLlmProtocolMeta(cachedResponse, {
								request_id: requestId,
								idempotency_mode: 'request_id',
								dedupe: 'hit',
								cache_ttl_s: ROOT_LLM_REQUEST_DEDUPE_TTL_S,
								cached_at: typeof cached['cached_at'] === 'string' ? cached['cached_at'] : null,
							}),
						)
					}
				} catch {}
			}
		}

		const r = await fetch('https://api.openai.com/v1/responses', {
			method: 'POST',
			headers: {
				'content-type': 'application/json',
				authorization: `Bearer ${apiKey}`,
			},
			body: JSON.stringify(openaiPayload),
		})

		const text = await r.text()
		let data: any = null
		if (text) {
			try {
				data = JSON.parse(text)
			} catch (e: any) {
				console.warn('llm upstream returned non-JSON payload', e)
			}
		}

		if (!r.ok) {
			return res
				.status(r.status)
				.json(data ?? { ok: false, error: 'llm_upstream_failed', status: r.status })
		}

		await recordRootMgmntLlmEvent({
			subnetId: decision.caller.subnetId,
			model,
			status: 'allowed',
		})

		if (requestId) {
			await redisClient.setEx(
				llmCacheKey(requestId),
				ROOT_LLM_REQUEST_DEDUPE_TTL_S,
				JSON.stringify({
					request_id: requestId,
					request_fingerprint: fingerprint,
					cached_at: new Date().toISOString(),
					response: data ?? { ok: true },
				}),
			)
		}
		const protocolMeta = requestId
			? {
				request_id: requestId,
				idempotency_mode: 'request_id',
				dedupe: 'miss',
				cache_ttl_s: ROOT_LLM_REQUEST_DEDUPE_TTL_S,
			}
			: null
		return res.json(protocolMeta ? attachLlmProtocolMeta(data ?? { ok: true }, protocolMeta) : (data ?? { ok: true }))
	} catch (error: any) {
		console.error('llm proxy failed', error)
		return res
			.status(502)
			.json({ ok: false, error: 'llm_proxy_failed', detail: String(error?.message ?? error) })
	}
})

// Prometheus metrics endpoint
/* import client from 'prom-client'
app.get('/metrics', async (_req, res) => {
	try {
		res.set('Content-Type', client.register.contentType)
		res.send(await client.register.metrics())
	} catch (e) {
		res.status(500).send(String(e))
	}
}) */

const rootRouter = express.Router()

rootRouter.post('/auth/owner/start', (req, res) => {
	let ownerId: string
	try {
		ownerId = requireJsonField(req.body, 'owner_id')
	} catch (error) {
		handleError(req, res, error, {
			status: 400,
			code: 'missing_field',
			params: { field: 'owner_id' },
		})
		return
	}
	const deviceCode = generateToken('dc')
	const userCode = generateUserCode()
	const expiresAt = new Date(Date.now() + 10 * 60 * 1000)
	const record: DeviceAuthorization = {
		ownerId,
		deviceCode,
		userCode,
		interval: 5,
		expiresAt,
		approved: false,
	}
	deviceAuthorizations.set(deviceCode, record)
	// Дополнительно сохраняем device_code в Redis для веб-сессий (WebAuthn / frontend)
	storeDeviceCode(redisClient, {
		device_code: deviceCode,
		user_code: userCode,
		owner_id: ownerId,
		// в dev-режиме owner_id совпадает с subnet_id
		subnet_id: ownerId,
		hub_id: undefined,
		// exp в секундах Unix
		exp: Math.floor(expiresAt.getTime() / 1000),
	}).catch((err) => {
		console.error('failed to store device_code for web session', err)
	})
	setTimeout(() => {
		const current = deviceAuthorizations.get(deviceCode)
		if (current) {
			current.approved = true
		}
	}, 1000).unref()
	res.json({
		device_code: deviceCode,
		user_code: userCode,
		verify_uri: OWNER_REGISTRATION_URL,
		verification_uri_complete: `${OWNER_REGISTRATION_URL}&user_code=${encodeURIComponent(userCode)}`,
		interval: record.interval,
		expires_in: Math.floor((expiresAt.getTime() - Date.now()) / 1000),
	})
})

rootRouter.post('/auth/owner/poll', (req, res) => {
	let deviceCode: string
	try {
		deviceCode = requireJsonField(req.body, 'device_code')
	} catch (error) {
		handleError(req, res, error, {
			status: 400,
			code: 'missing_field',
			params: { field: 'device_code' },
		})
		return
	}
	const auth = deviceAuthorizations.get(deviceCode)
	if (!auth) {
		respondError(req, res, 400, 'invalid_device_code')
		return
	}
	if (auth.expiresAt.getTime() <= Date.now()) {
		deviceAuthorizations.delete(deviceCode)
		respondError(req, res, 400, 'expired_token')
		return
	}
	if (!auth.approved) {
		respondError(req, res, 400, 'authorization_pending')
		return
	}
	const owner = ensureOwnerRecord(auth.ownerId)
	const refreshToken = updateRefreshToken(owner)
	const { token: accessToken, expiresAt } = issueAccessToken(owner)
	deviceAuthorizations.delete(deviceCode)
	res.json({
		access_token: accessToken,
		refresh_token: refreshToken,
		expires_at: expiresAt.toISOString(),
		subject: owner.subject,
		scopes: owner.scopes,
		owner_id: owner.ownerId,
		hub_ids: Array.from(owner.hubs.keys()),
	})
})

rootRouter.post('/auth/owner/refresh', (req, res) => {
	let refreshToken: string
	try {
		refreshToken = requireJsonField(req.body, 'refresh_token')
	} catch (error) {
		handleError(req, res, error, {
			status: 400,
			code: 'missing_field',
			params: { field: 'refresh_token' },
		})
		return
	}
	const ownerId = refreshIndex.get(refreshToken)
	if (!ownerId) {
		respondError(req, res, 401, 'invalid_refresh_token')
		return
	}
	const owner = owners.get(ownerId)
	if (!owner || owner.refreshToken !== refreshToken) {
		respondError(req, res, 401, 'invalid_refresh_token')
		return
	}
	const { token, expiresAt } = issueAccessToken(owner)
	res.json({ access_token: token, expires_at: expiresAt.toISOString() })
})

rootRouter.get('/whoami', authenticateOwnerBearer, (req, res) => {
	const owner = req.ownerAuth!
	res.json({
		subject: owner.subject,
		owner_id: owner.ownerId,
		roles: ['owner'],
		scopes: owner.scopes,
		hub_ids: Array.from(owner.hubs.keys()),
	})
})

rootRouter.get('/owner/hubs', authenticateOwnerBearer, (req, res) => {
	const owner = req.ownerAuth!
	const hubs = Array.from(owner.hubs.values()).map((hub) => ({
		hub_id: hub.hubId,
		owner_id: hub.ownerId,
		created_at: hub.createdAt.toISOString(),
		last_seen: hub.lastSeen.toISOString(),
		revoked: hub.revoked,
	}))
	res.json(hubs)
})

rootRouter.post('/owner/hubs', authenticateOwnerBearer, (req, res) => {
	const owner = req.ownerAuth!
	let hubId: string
	try {
		hubId = requireJsonField(req.body, 'hub_id')
	} catch (error) {
		handleError(req, res, error, {
			status: 400,
			code: 'missing_field',
			params: { field: 'hub_id' },
		})
		return
	}
	let hub = owner.hubs.get(hubId)
	if (!hub) {
		hub = {
			hubId,
			ownerId: owner.ownerId,
			createdAt: new Date(),
			lastSeen: new Date(),
			revoked: false,
		}
		owner.hubs.set(hubId, hub)
	} else if (hub.revoked) {
		hub.revoked = false
	}
	hub.lastSeen = new Date()
	owner.updatedAt = new Date()
	res.status(201).json({ hub_id: hub.hubId, owner_id: hub.ownerId })
})

rootRouter.post('/pki/enroll', authenticateOwnerBearer, (req, res) => {
	const owner = req.ownerAuth!
	let hubId: string
	let csrPem: string
	try {
		hubId = requireJsonField(req.body, 'hub_id')
		csrPem = requireJsonField(req.body, 'csr_pem')
	} catch (error) {
		handleError(req, res, error, { status: 400, code: 'invalid_request' })
		return
	}
	const ttlDays = parseTtl(
		(req.body as Record<string, unknown> | undefined)?.['ttl']
	)
	const hub = owner.hubs.get(hubId)
	if (!hub || hub.revoked) {
		respondError(req, res, 404, 'hub_not_registered')
		return
	}
	hub.lastSeen = new Date()
	try {
		const csrPemRaw =
			typeof req.body?.csr_pem === 'string' ? req.body.csr_pem : null
		const csrPem = csrPemRaw.replace(/\r\n/g, '\n').trim() + '\n'
		const result = certificateAuthority.issueClientCertificate({
			csrPem,
			subject: {
				commonName: `hub:${hubId}`,
				organizationName: `owner:${owner.ownerId}`,
			},
			validityDays: ttlDays,
		})
		res.json({ cert_pem: result.certificatePem, chain_pem: CA_CERT_PEM })
	} catch (error) {
		console.error('pki enrollment failed', error)
		handleError(req, res, error, {
			status: 400,
			code: 'certificate_issue_failed',
		})
	}
})

if (process.env['DEBUG_ENDPOINTS'] === 'true') {
	rootRouter.get('/debug/owners', (_req, res) => {
		const payload = Array.from(owners.values()).map((owner) => ({
			owner_id: owner.ownerId,
			subjects: owner.subject ? [owner.subject] : [],
			hubs_count: owner.hubs.size,
			created_at: owner.createdAt.toISOString(),
			updated_at: owner.updatedAt.toISOString(),
		}))
		res.json(payload)
	})
	rootRouter.get('/debug/hubs', (_req, res) => {
		const hubs: Array<{
			hub_id: string
			owner_id: string
			created_at: string
			last_seen: string
			key_fp: string
		}> = []
		for (const owner of owners.values()) {
			for (const hub of owner.hubs.values()) {
				hubs.push({
					hub_id: hub.hubId,
					owner_id: hub.ownerId,
					created_at: hub.createdAt.toISOString(),
					last_seen: hub.lastSeen.toISOString(),
					key_fp: '',
				})
			}
		}
		res.json(hubs)
	})
}

app.use('/v1', rootRouter)

// --- IO (Telegram) wiring ---
try {
	if (process.env['PG_URL']) await ensureTgSchema()
} catch { }
let ioBus: NatsBus | null = null
if (
	(process.env['IO_BUS_KIND'] || 'local').toLowerCase() === 'nats' &&
	process.env['NATS_URL']
) {
	try {
		ioBus = new NatsBus(process.env['NATS_URL']!)
		await ioBus.connect()
		console.log(`[io] NATS connected`)
		// subscribe to outbound for a single configured bot
		const botId = process.env['BOT_ID'] || 'main-bot'
		const { TelegramSender } = await import('./io/telegram/sender.js')
		const tgToken = process.env['TG_BOT_TOKEN'] || ''
		if (!tgToken) {
			console.warn('[io] TG_BOT_TOKEN is not configured; Telegram outbound will fail')
		}
		const sender = new TelegramSender(tgToken)
		console.log(`[io] Subscribing to tg.output.${botId}.>`)
		await ioBus.subscribe_output(botId, async (subject, data) => {
			try {
				const text = new TextDecoder().decode(data)
				const payload = JSON.parse(text)
				console.log(`[io] Outbound received on ${subject}`)
				await sender.send(payload)
			} catch (e) {
				const err = String((e as any)?.message ?? e)
				console.warn(`[io] Telegram outbound failed on ${subject}: ${err}`)
				try {
					await ioBus!.publish_dlq('output', { error: err, subject })
				} catch { }
			}
		})
		// Backward compatibility: legacy hubs may publish to io.tg.out
		console.log(`[io] Subscribing to legacy io.tg.out`)
		await ioBus.subscribe_compat_out(async (subject, data) => {
			try {
				const text = new TextDecoder().decode(data)
				const payload = JSON.parse(text)
				console.log(`[io] Legacy outbound received on ${subject}`)
				await sender.send(payload)
			} catch (e) {
				const err = String((e as any)?.message ?? e)
				console.warn(`[io] Telegram legacy outbound failed on ${subject}: ${err}`)
				try {
					await ioBus!.publish_dlq('output', { error: err, subject })
				} catch { }
			}
		})
		// Optional: debug taps (comma-separated subjects) e.g. IO_TAP_SUBJECTS="tg.input.>,tg.output.>,io.tg.out"
		const tap = (process.env['IO_TAP_SUBJECTS'] || '').trim()
		if (tap) {
			for (const subj of tap
				.split(',')
				.map((s) => s.trim())
				.filter(Boolean)) {
				console.log(`[io] Tap subscribe ${subj}`)
				await ioBus!.subscribe(subj, async (s, data) => {
					const txt = new TextDecoder().decode(data)
					console.log(`[io][tap] ${s} ${txt.slice(0, 512)}`)
				})
			}
		}

		// Optional legacy bridge: republish tg.input.<hub> text to io.tg.in.<hub>.text
		if ((process.env['IO_BRIDGE_TG_INPUT_TO_LEGACY'] || '0') === '1') {
			console.log(
				'[io] Legacy bridge enabled: tg.input.* -> io.tg.in.*.text'
			)
			await ioBus!.subscribe('tg.input.>', async (subject, data) => {
				try {
					const txt = new TextDecoder().decode(data)
					const env = JSON.parse(txt)
					const hubMatch = subject.match(/^tg\.input\.(.+)$/)
					const hub = hubMatch ? hubMatch[1] : env?.meta?.hub_id || ''
					if (hub && env?.payload?.type === 'text') {
						const e = env.payload
						const legacy = {
							text: e?.payload?.text || e?.text || '',
							chat_id: Number(e?.chat_id || 0),
							tg_msg_id: Number(
								e?.payload?.meta?.msg_id || e?.meta?.msg_id || 0
							),
							route: { via: 'session' },
							meta: { is_command: false },
						}
						const legacySubj = `io.tg.in.${hub}.text`
						await ioBus!.publish_subject(legacySubj, legacy)
						console.log(`[io] bridged ${subject} -> ${legacySubj}`)
					}
				} catch {
					/* ignore */
				}
			})
		}
	} catch (e) {
		console.error('[io] NATS init failed', e)
		ioBus = null
	}
}
installTelegramWebhookRoutes(app, ioBus)
installPairingApi(app)
try {
	const natsAuthModule = await import('./io/bus/natsAuth.js')
	await natsAuthModule.installNatsAuth(app)
	console.log('[io] nats auth callout installed')
} catch (e) {
	console.error('[io] nats auth callout init failed', e)
	throw e
}

// Install WS->NATS proxy for hubs (accepts NATS WS handshake, rewrites creds)
try {
	installWsNatsProxy(server)
	wsNatsProxyReady = true
} catch (e) {
	wsNatsProxyReady = false
	console.error('ws nats proxy init failed', e)
}

// Install Browser->Hub routing proxy (HTTP + WS) over NATS.
// This is the "root proxy" fallback path; it keeps browsers on api.inimatic.com while hubs stay behind NAT.
try {
	if (process.env['NATS_URL']) {
		const { installHubRouteProxy } = await import('./io/bus/hubRouteProxy.js')
		installHubRouteProxy(app, server, {
			redis: redisClient,
			natsUrl: process.env['NATS_URL']!,
			sessionJwtSecret: WEB_SESSION_JWT_SECRET,
			rootToken: ROOT_TOKEN,
		})
		hubRouteProxyReady = true
		console.log('[route] hub proxy installed')
	} else {
		hubRouteProxyReady = false
		console.warn('[route] NATS_URL missing; hub proxy disabled')
	}
} catch (e) {
	hubRouteProxyReady = false
	console.error('[route] hub proxy init failed', e)
}

// Send a message to Telegram resolving hub_id -> chat_id/bot_id via pairing store
app.post('/io/tg/send', async (req, res) => {
	try {
		const text = String((req.body as any)?.text || '')
		const hub_id = String((req.body as any)?.hub_id || '')
		const explicitBot =
			typeof req.body?.bot_id === 'string' ? String(req.body.bot_id) : ''
		const explicitChat =
			typeof req.body?.chat_id === 'string'
				? String(req.body.chat_id)
				: ''
		if (!hub_id && !explicitChat)
			return res
				.status(400)
				.json({ ok: false, error: 'hub_id_or_chat_id_required' })
		if (!text)
			return res.status(400).json({ ok: false, error: 'text_required' })

		const activeBotId = process.env['BOT_ID'] || 'adaos_bot'
		let bot_id = explicitBot || activeBotId
		let chat_id = explicitChat
		if (!chat_id) {
			const { tgLinkGet } = await import('./io/pairing/store.js')
			const link = await tgLinkGet(hub_id)
			if (!link)
				return res
					.status(404)
					.json({ ok: false, error: 'pairing_not_found', hub_id })
			chat_id = link.chat_id
			if (!explicitBot && link.bot_id && String(link.bot_id) !== String(activeBotId)) {
				console.warn(
					`[io] tg pairing bot mismatch for hub_id=${hub_id}: link.bot_id=${String(link.bot_id)} BOT_ID=${String(activeBotId)}; using BOT_ID`
				)
			}
		}

		function displaySubnetAlias(aliasValue: unknown, hubIdValue: unknown): string | undefined {
			const aliasText = String(aliasValue || '').trim()
			const hubText = String(hubIdValue || '').trim()
			if (aliasText && !/^hub(?:-\d+)?$/i.test(aliasText)) return aliasText
			return hubText || aliasText || undefined
		}

		// Resolve human-friendly alias for prefixing in Telegram outbox
		let alias: string | undefined
		try {
			const { listBindings } = await import('./db/tg.repo.js')
			const binds = await listBindings(Number(chat_id))
			alias = displaySubnetAlias(
				(binds || []).find((b) => String(b.hub_id) === String(hub_id))?.alias,
				hub_id,
			)
		} catch {
			alias = displaySubnetAlias(undefined, hub_id)
		}

		if (!ioBus)
			return res
				.status(503)
				.json({ ok: false, error: 'io_bus_unavailable' })
		const payload = {
			alias,
			target: {
				bot_id,
				hub_id: hub_id || process.env['DEFAULT_HUB'] || 'hub-a',
				chat_id,
			},
			messages: [{ type: 'text', text }],
		}
		const subject = `tg.output.${bot_id}.chat.${chat_id}`
		await ioBus.publishSubject(subject, payload)
		return res.status(202).json({ ok: true, subject })
	} catch (e) {
		console.error('tg/send failed', e)
		return res.status(500).json({ ok: false })
	}
})

app.post('/v1/bootstrap_token', async (req, res) => {
	const token = req.header('X-Root-Token') ?? ''
	if (!token || token !== ROOT_TOKEN) {
		respondError(req, res, 401, 'unauthorized')
		return
	}
	const meta = (
		typeof req.body === 'object' && req.body !== null ? req.body : {}
	) as Record<string, unknown>
	const oneTimeToken = randomBytes(24).toString('hex')
	const expiresAt = new Date(Date.now() + BOOTSTRAP_TOKEN_TTL_SECONDS * 1000)
	await redisClient.setEx(
		`bootstrap:${oneTimeToken}`,
		BOOTSTRAP_TOKEN_TTL_SECONDS,
		JSON.stringify({ issued_at: new Date().toISOString(), ...meta })
	)
	res.status(201).json({
		one_time_token: oneTimeToken,
		expires_at: expiresAt.toISOString(),
	})
})

// Developer endpoint: fetch recent Root logs (captured from stdout/stderr).
// Protected by ROOT_TOKEN.
app.get('/v1/dev/logs', async (req, res) => {
	if (process.env['DEBUG_ENDPOINTS'] !== 'true') {
		return res.status(404).json({ ok: false, error: 'debug_endpoints_disabled' })
	}
	const token = req.header('X-Root-Token') ?? ''
	if (!token || token !== ROOT_TOKEN) {
		return res.status(401).json({ ok: false, error: 'unauthorized' })
	}
	const minutesRaw = Number(String(req.query['minutes'] ?? '30'))
	const minutes = Number.isFinite(minutesRaw)
		? Math.max(1, Math.min(12 * 60, Math.floor(minutesRaw)))
		: 30
	const limitRaw = Number(String(req.query['limit'] ?? '2000'))
	const limit = Number.isFinite(limitRaw)
		? Math.max(1, Math.min(50_000, Math.floor(limitRaw)))
		: 2000
	const contains =
		typeof req.query['contains'] === 'string'
			? String(req.query['contains'])
			: null
	const hubId =
		typeof req.query['hub_id'] === 'string'
			? String(req.query['hub_id'])
			: null
	const sinceMs = Date.now() - minutes * 60 * 1000
	const items = queryRootLogs({ sinceMs, limit, contains, hubId })
	return res.json({ ok: true, minutes, limit, since_ms: sinceMs, items })
})

// Developer endpoint: list/tail log files from a shared directory mounted into the backend container.
// Protected by ROOT_TOKEN.
app.get('/v1/dev/log_files', async (req, res) => {
	if (process.env['DEBUG_ENDPOINTS'] !== 'true') {
		return res.status(404).json({ ok: false, error: 'debug_endpoints_disabled' })
	}
	const token = req.header('X-Root-Token') ?? ''
	if (!token || token !== ROOT_TOKEN) {
		return res.status(401).json({ ok: false, error: 'unauthorized' })
	}
	try {
		const contains =
			typeof req.query['contains'] === 'string'
				? String(req.query['contains'])
				: null
		const limitRaw = Number(String(req.query['limit'] ?? '500'))
		const limit = Number.isFinite(limitRaw) ? limitRaw : 500
		const items = await listLogFiles({ contains, limit })
		return res.json({ ok: true, items })
	} catch (e: any) {
		return res.status(500).json({ ok: false, error: String(e?.message ?? e) })
	}
})

app.get('/v1/dev/log_tail', async (req, res) => {
	if (process.env['DEBUG_ENDPOINTS'] !== 'true') {
		return res.status(404).json({ ok: false, error: 'debug_endpoints_disabled' })
	}
	const token = req.header('X-Root-Token') ?? ''
	if (!token || token !== ROOT_TOKEN) {
		return res.status(401).json({ ok: false, error: 'unauthorized' })
	}
	try {
		const file = typeof req.query['file'] === 'string' ? String(req.query['file']) : ''
		const linesRaw = Number(String(req.query['lines'] ?? '200'))
		const lines = Number.isFinite(linesRaw) ? linesRaw : 200
		const maxBytesRaw = Number(String(req.query['max_bytes'] ?? '2000000'))
		const maxBytes = Number.isFinite(maxBytesRaw) ? maxBytesRaw : 2_000_000
		const result = await tailLogFile({ relPath: file, lines, maxBytes })
		return res.json({ ok: true, ...result })
	} catch (e: any) {
		return res.status(400).json({ ok: false, error: String(e?.message ?? e) })
	}
})

async function dispatchHubCoreUpdate(
	path: '/api/admin/update/start' | '/api/admin/update/rollback',
	payload: Record<string, unknown>,
	hubIds: string[],
): Promise<Array<Record<string, unknown>>> {
	const base = `http://127.0.0.1:${PORT}`
	const uniqueHubIds = Array.from(new Set(hubIds.map((item) => String(item || '').trim()).filter(Boolean)))
	const results = await Promise.all(
		uniqueHubIds.map(async (hubId) => {
			const url = `${base}/hubs/${encodeURIComponent(hubId)}${path}`
			try {
				const response = await fetch(url, {
					method: 'POST',
					headers: {
						'content-type': 'application/json',
						'X-Root-Token': ROOT_TOKEN,
					},
					body: JSON.stringify(payload),
				})
				let body: unknown = null
				try {
					body = await response.json()
				} catch {
					body = null
				}
				return {
					hub_id: hubId,
					ok: response.ok,
					status: response.status,
					body,
				}
			} catch (error) {
				return {
					hub_id: hubId,
					ok: false,
					error: String((error as Error)?.message || error),
				}
			}
		}),
	)
	return results
}

app.post('/v1/hubs/core_update/start', async (req, res) => {
	if (!requireRootToken(req, res)) return
	const body = typeof req.body === 'object' && req.body !== null ? req.body : {}
	const connectedHubIds = listActiveHubIds()
	const explicit = Array.isArray((body as any).hub_ids) ? (body as any).hub_ids : []
	const hubIds = explicit.length ? explicit : connectedHubIds
	if (!hubIds.length) {
		return res.json({ ok: true, hub_ids: [], connected_hub_ids: connectedHubIds, results: [] })
	}
	const payload = {
		target_rev: typeof (body as any).target_rev === 'string' ? String((body as any).target_rev) : '',
		target_version: typeof (body as any).target_version === 'string' ? String((body as any).target_version) : '',
		reason: typeof (body as any).reason === 'string' ? String((body as any).reason) : 'root.core_update',
		countdown_sec: Number((body as any).countdown_sec ?? 60),
		drain_timeout_sec: Number((body as any).drain_timeout_sec ?? 10),
		signal_delay_sec: Number((body as any).signal_delay_sec ?? 0.25),
	}
	const results = await dispatchHubCoreUpdate('/api/admin/update/start', payload, hubIds)
	return res.json({ ok: true, hub_ids: hubIds, connected_hub_ids: connectedHubIds, results })
})

app.post('/v1/hubs/core_update/rollback', async (req, res) => {
	if (!requireRootToken(req, res)) return
	const body = typeof req.body === 'object' && req.body !== null ? req.body : {}
	const connectedHubIds = listActiveHubIds()
	const explicit = Array.isArray((body as any).hub_ids) ? (body as any).hub_ids : []
	const hubIds = explicit.length ? explicit : connectedHubIds
	if (!hubIds.length) {
		return res.json({ ok: true, hub_ids: [], connected_hub_ids: connectedHubIds, results: [] })
	}
	const payload = {
		reason: typeof (body as any).reason === 'string' ? String((body as any).reason) : 'root.core_rollback',
		countdown_sec: Number((body as any).countdown_sec ?? 0),
		drain_timeout_sec: Number((body as any).drain_timeout_sec ?? 10),
		signal_delay_sec: Number((body as any).signal_delay_sec ?? 0.25),
	}
	const results = await dispatchHubCoreUpdate('/api/admin/update/rollback', payload, hubIds)
	return res.json({ ok: true, hub_ids: hubIds, connected_hub_ids: connectedHubIds, results })
})

app.get('/v1/hubs/core_update/reports', async (req, res) => {
	if (!requireRootToken(req, res)) return
	const hubId = typeof req.query['hub_id'] === 'string' ? String(req.query['hub_id']).trim() : ''
	try {
		if (hubId) {
			const raw = await redisClient.hGet(ROOT_CORE_UPDATE_REPORTS_HASH, hubId)
			return res.json({ ok: true, items: raw ? [{ hub_id: hubId, report: JSON.parse(raw) }] : [] })
		}
		const rawItems = await redisClient.hGetAll(ROOT_CORE_UPDATE_REPORTS_HASH)
		const items = Object.entries(rawItems).map(([id, raw]) => {
			let report: unknown = raw
			try {
				report = JSON.parse(raw)
			} catch {}
			return { hub_id: id, report }
		})
		return res.json({ ok: true, items })
	} catch (error) {
		return res.status(500).json({ ok: false, error: String((error as Error)?.message || error) })
	}
})

app.get('/v1/hubs/control/reports', async (req, res) => {
	if (!requireRootToken(req, res)) return
	const hubId = typeof req.query['hub_id'] === 'string' ? String(req.query['hub_id']).trim() : ''
	try {
		if (hubId) {
			const raw = await redisClient.hGet(ROOT_HUB_CONTROL_REPORTS_HASH, hubId)
			return res.json({ ok: true, items: raw ? [{ hub_id: hubId, report: JSON.parse(raw) }] : [] })
		}
		const rawItems = await redisClient.hGetAll(ROOT_HUB_CONTROL_REPORTS_HASH)
		const items = Object.entries(rawItems).map(([id, raw]) => {
			let report: unknown = raw
			try {
				report = JSON.parse(raw)
			} catch {}
			return { hub_id: id, report }
		})
		return res.json({ ok: true, items })
	} catch (error) {
		return res.status(500).json({ ok: false, error: String((error as Error)?.message || error) })
	}
})

app.get('/v1/hubs/core_update/subnets', async (req, res) => {
	if (!requireRootToken(req, res)) return
	try {
		const rawItems = await redisClient.hGetAll(ROOT_CORE_UPDATE_SUBNETS_HASH)
		const items = Object.entries(rawItems).map(([id, raw]) => {
			let state: unknown = raw
			try {
				state = JSON.parse(raw)
			} catch {}
			return { hub_id: id, state }
		})
		return res.json({ ok: true, items })
	} catch (error) {
		return res.status(500).json({ ok: false, error: String((error as Error)?.message || error) })
	}
})

app.post('/v1/subnets/register', async (req, res) => {
	const t0 = Date.now()
	console.log('register: start')

	const bootstrapToken = req.header('X-Bootstrap-Token') ?? ''
	if (!bootstrapToken) {
		/* ... */
	}

	let bootstrapMeta: Record<string, unknown> = {}
	try {
		bootstrapMeta = await useBootstrapToken(bootstrapToken)
		console.log('register: bootstrap token OK, dt=%dms', Date.now() - t0)
	} catch (e) {
		/* ... */
	}

	const csrPemRaw =
		typeof req.body?.csr_pem === 'string' ? req.body.csr_pem : null
	if (!csrPemRaw) {
		/* ... */
	}
	const csrPem = csrPemRaw.replace(/\r\n/g, '\n').trim() + '\n'

	const fingerprintRaw =
		typeof bootstrapMeta['fingerprint'] === 'string'
			? (bootstrapMeta['fingerprint'] as string)
			: undefined
	let reused = false
	let subnetId = ''
	if (fingerprintRaw) {
		try {
			const existingSubnet = await redisClient.hGet(
				HUB_FINGERPRINT_HASH,
				fingerprintRaw
			)
			if (existingSubnet) {
				subnetId = existingSubnet
				reused = true
				console.log(
					'register: reusing subnet %s for fingerprint %s',
					existingSubnet,
					fingerprintRaw
				)
			}
		} catch (error) {
			console.warn('register: failed to lookup fingerprint reuse', {
				fingerprint: fingerprintRaw,
				error,
			})
		}
	}
	if (!subnetId) {
		subnetId = generateSubnetId()
	}
	let certPem: string
	try {
		certPem = certificateAuthority.issueClientCertificate({
			csrPem,
			subject: {
				commonName: `subnet:${subnetId}`,
				organizationName: `subnet:${subnetId}`,
			},
		}).certificatePem
		console.log('register: cert issued v2, dt=%dms', Date.now() - t0)
	} catch (e) {
		console.error('register: issue cert failed:', (e as any)?.message)
		return handleError(req, res, e, {
			status: 400,
			code: 'certificate_issue_failed',
		})
	}

	try {
		console.log('register: on before ensureSubnet')
		await forgeManager.ensureSubnet(subnetId)
		console.log('register: forge ensured, dt=%dms', Date.now() - t0)
	} catch (e) {
		console.error('register: forge ensureSubnet failed:', e)
		return handleError(req, res, e, {
			status: 500,
			code: 'draft_store_failed',
		})
	}

	await redisClient.hSet(
		'root:subnets',
		subnetId,
		JSON.stringify({ subnet_id: subnetId, created_at: Date.now() })
	)
	if (fingerprintRaw) {
		try {
			await redisClient.hSet(
				HUB_FINGERPRINT_HASH,
				fingerprintRaw,
				subnetId
			)
		} catch (error) {
			console.warn('register: failed to store fingerprint mapping', {
				fingerprint: fingerprintRaw,
				error,
			})
		}
	}
	console.log('register: redis saved, dt=%dms', Date.now() - t0)

	res.status(201).json({
		subnet_id: subnetId,
		cert_pem: certPem,
		ca_pem: CA_CERT_PEM,
		forge: { repo: FORGE_GIT_URL, path: `subnets/${subnetId}` },
		reused,
	})
	console.log('register: done, total=%dms', Date.now() - t0)
})

const JOIN_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'

function normalizeJoinCode(code: string): string {
	return String(code || '')
		.trim()
		.toUpperCase()
		.replace(/[^A-Z0-9]/g, '')
}

function formatJoinCode(code: string): string {
	const norm = normalizeJoinCode(code)
	if (!norm) return ''
	const mid = Math.floor(norm.length / 2)
	if (mid <= 0 || mid >= norm.length) return norm
	return `${norm.slice(0, mid)}-${norm.slice(mid)}`
}

function hashJoinCode(code: string): string {
	return createHash('sha256').update(normalizeJoinCode(code)).digest('hex')
}

function generateJoinCode(length: number): string {
	const len = Math.max(8, Math.min(12, Math.floor(Number(length) || 8)))
	const bytes = randomBytes(len)
	let raw = ''
	for (let i = 0; i < len; i++) {
		raw += JOIN_CODE_ALPHABET[bytes[i] % JOIN_CODE_ALPHABET.length]
	}
	return formatJoinCode(raw)
}

function resolveJoinSessionTtlSeconds(): number {
	const raw = (process.env['ADAOS_JOIN_SESSION_TTL_SECONDS'] || '').trim()
	if (raw) {
		const v = Number.parseInt(raw, 10)
		if (Number.isFinite(v) && v > 0) return v
	}
	// Default: 30 days
	return 30 * 24 * 60 * 60
}

const JOIN_SESSION_TTL_SECONDS = resolveJoinSessionTtlSeconds()

function authenticateOwnerBearerInline(req: express.Request): OwnerRecord | null {
	const header = req.header('Authorization') ?? ''
	const token = header.startsWith('Bearer ')
		? header.slice('Bearer '.length).trim()
		: ''
	if (!token || !accessIndex.has(token)) {
		return null
	}
	const ownerId = accessIndex.get(token)!
	const owner = owners.get(ownerId)
	if (!owner) return null
	if (
		owner.accessToken !== token ||
		owner.accessExpiresAt.getTime() <= Date.now()
	) {
		return null
	}
	return owner
}

// Create a short one-time join-code on Root so member nodes can join without a long-lived token.
// Auth: either hub mTLS certificate (preferred) or owner bearer session (dev/browser).
// Root returns a rendezvous URL for the hub via the Root proxy: /hubs/<subnet_id>/...
app.post('/v1/subnets/join-code', async (req, res) => {
	try {
		const bodySubnet =
			typeof req.body?.subnet_id === 'string' ? String(req.body.subnet_id).trim() : ''

		const identity = getClientIdentity(req)
		const hubIdentity = identity && identity.type === 'hub' ? identity : null
		const owner = authenticateOwnerBearerInline(req)
		const rootToken = String(req.header('X-Root-Token') || '').trim()
		const haveRootToken = Boolean(rootToken && rootToken === ROOT_TOKEN)
		const subnet_id = (() => {
			if (hubIdentity) return hubIdentity.subnetId
			return bodySubnet
		})()

		if (!subnet_id) {
			return res.status(400).json({ ok: false, error: 'subnet_id_required' })
		}

		// If hub mTLS is used, prevent forging cross-subnet codes.
		if (hubIdentity && bodySubnet && bodySubnet !== hubIdentity.subnetId) {
			return res.status(403).json({ ok: false, error: 'forbidden' })
		}

		// If owner bearer is used, ensure the owner has access to this hub/subnet id.
		if (!hubIdentity && !owner && !haveRootToken) {
			return res.status(401).json({ ok: false, error: 'unauthorized' })
		}
		if (!hubIdentity && owner) {
			const hub = owner.hubs.get(subnet_id)
			if (!hub || hub.revoked) {
				return res.status(404).json({ ok: false, error: 'hub_not_registered' })
			}
			hub.lastSeen = new Date()
		}
		if (!hubIdentity && !owner && haveRootToken) {
			// Best-effort: ensure subnet exists (registered) before issuing codes.
			// Do not hard-fail: redis state may be wiped on redeploy while hubs are still online and routable via ws-nats-proxy.
			try {
				const existing = await redisClient.hGet('root:subnets', subnet_id)
				if (!existing) {
					await redisClient.hSet(
						'root:subnets',
						subnet_id,
						JSON.stringify({
							subnet_id,
							created_at: Date.now(),
							source: 'root_token_join_code',
						})
					)
				}
			} catch {
				// ignore and proceed
			}
		}

		const ttlMinutesRaw = Number(req.body?.ttl_minutes ?? 15)
		const ttlMinutes = Number.isFinite(ttlMinutesRaw)
			? Math.max(1, Math.min(60, Math.floor(ttlMinutesRaw)))
			: 15
		const lengthRaw = Number(req.body?.length ?? 8)
		const length = Number.isFinite(lengthRaw)
			? Math.max(8, Math.min(12, Math.floor(lengthRaw)))
			: 8

		const code = generateJoinCode(length)
		const key = `join_code:${hashJoinCode(code)}`
		const expiresAt = new Date(Date.now() + ttlMinutes * 60 * 1000)
		await redisClient.setEx(
			key,
			ttlMinutes * 60,
			JSON.stringify({
				subnet_id,
				issued_by: 'root',
				auth: hubIdentity
					? { method: 'mtls', subnet_id }
					: owner
						? { method: 'owner_bearer', owner_id: owner.ownerId }
						: haveRootToken
							? { method: 'root_token' }
						: { method: 'unknown' },
				created_at_utc: new Date().toISOString(),
				expires_at_utc: expiresAt.toISOString(),
			})
		)

		return res.status(200).json({ ok: true, code, expires_at_utc: expiresAt.toISOString() })
	} catch (error) {
		return handleError(req, res, error, { status: 500, code: 'internal_error' })
	}
})

// Consume a join-code on Root and return connection parameters for a member node.
// This returns a Root-proxy hub URL and an opaque session token (stored in Redis) that authorizes
// subsequent /hubs/<subnet_id>/... requests.
app.post('/v1/subnets/join', async (req, res) => {
	try {
		const code = typeof req.body?.code === 'string' ? String(req.body.code).trim() : ''
		if (!code) return res.status(422).json({ detail: 'code is required' })

		const joinKey = `join_code:${hashJoinCode(code)}`
		const raw = await redisClient.get(joinKey)
		if (!raw) return res.status(404).json({ detail: 'join-code not found' })
		await redisClient.del(joinKey)

		let rec: any = {}
		try {
			rec = JSON.parse(raw)
		} catch {
			rec = {}
		}
		const subnet_id = typeof rec?.subnet_id === 'string' ? String(rec.subnet_id).trim() : ''
		if (!subnet_id) return res.status(500).json({ detail: 'invalid join-code record' })

		const sessionToken = randomBytes(32).toString('hex')
		await redisClient.setEx(
			`session:jwt:${sessionToken}`,
			JOIN_SESSION_TTL_SECONDS,
			JSON.stringify({
				sid: `join_${uuidv4().replace(/-/g, '')}`,
				hub_id: subnet_id,
				subnet_id,
				stage: 'JOIN_CODE',
				node_id: typeof req.body?.node_id === 'string' ? String(req.body.node_id).trim() : undefined,
				hostname: typeof req.body?.hostname === 'string' ? String(req.body.hostname).trim() : undefined,
				issued_at_utc: new Date().toISOString(),
				expires_at_utc: new Date(Date.now() + JOIN_SESSION_TTL_SECONDS * 1000).toISOString(),
			})
		)

		const host = String(req.get('host') || '').trim()
		const root_url = `${ROOT_SERVER_PROTO}://${host}`
		const hub_url = `${root_url}/hubs/${encodeURIComponent(subnet_id)}`

		return res.status(200).json({
			ok: true,
			subnet_id,
			token: sessionToken,
			root_url,
			hub_url,
			diagnostics: {
				issued_by: rec?.issued_by ?? 'root',
				code_created_at_utc: rec?.created_at_utc ?? null,
				code_expires_at_utc: rec?.expires_at_utc ?? null,
				session_expires_at_utc: new Date(Date.now() + JOIN_SESSION_TTL_SECONDS * 1000).toISOString(),
			},
		})
	} catch (error) {
		return handleError(req, res, error, { status: 500, code: 'internal_error' })
	}
})

app.post('/v1/nodes/register', async (req, res) => {
	const bootstrapToken = req.header('X-Bootstrap-Token') ?? ''
	let subnetId: string | undefined

	if (bootstrapToken) {
		try {
			await useBootstrapToken(bootstrapToken)
		} catch (error) {
			handleError(req, res, error, {
				status: 401,
				code: 'invalid_bootstrap_token',
			})
			return
		}
		const bodySubnet =
			typeof req.body?.subnet_id === 'string'
				? req.body.subnet_id
				: undefined
		if (!bodySubnet) {
			respondError(req, res, 400, 'subnet_required_with_bootstrap')
			return
		}
		subnetId = bodySubnet
	} else {
		const identity = getClientIdentity(req)
		if (!identity || identity.type !== 'hub') {
			respondError(req, res, 401, 'hub_certificate_required')
			return
		}
		subnetId = identity.subnetId
		const bodySubnet =
			typeof req.body?.subnet_id === 'string'
				? req.body.subnet_id
				: undefined
		if (bodySubnet && bodySubnet !== subnetId) {
			respondError(req, res, 400, 'subnet_certificate_mismatch')
			return
		}
	}

	if (!subnetId) {
		respondError(req, res, 400, 'subnet_required')
		return
	}

	const csrPem =
		typeof req.body?.csr_pem === 'string' ? req.body.csr_pem : null
	if (!csrPem) {
		respondError(req, res, 400, 'csr_required')
		return
	}

	const nodeId = generateNodeId()
	let certPem: string
	try {
		const csrPemRaw =
			typeof req.body?.csr_pem === 'string' ? req.body.csr_pem : null
		const csrPem = csrPemRaw.replace(/\r\n/g, '\n').trim() + '\n'
		const result = certificateAuthority.issueClientCertificate({
			csrPem,
			subject: {
				commonName: `node:${nodeId}`,
				organizationName: `subnet:${subnetId}`,
			},
		})
		certPem = result.certificatePem
	} catch (error) {
		console.error('node certificate issue failed', error)
		handleError(req, res, error, {
			status: 400,
			code: 'certificate_issue_failed',
		})
		return
	}

	await forgeManager.ensureSubnet(subnetId)
	await forgeManager.ensureNode(subnetId, nodeId)
	await redisClient.hSet(
		'root:nodes',
		nodeId,
		JSON.stringify({
			node_id: nodeId,
			subnet_id: subnetId,
			created_at: Date.now(),
		})
	)

	res.status(201).json({
		node_id: nodeId,
		subnet_id: subnetId,
		cert_pem: certPem,
		ca_pem: CA_CERT_PEM,
	})
})

const mtlsRouter = express.Router()

function parseDn(dn: string): { cn?: string; o?: string } {
	// nginx может отдавать DN в двух форматах:
	//   1) RFC2253-подобный: "CN=subnet:sn_xxx,O=subnet:sn_xxx"
	//   2) slash-style:      "/CN=subnet:sn_xxx/O=subnet:sn_xxx/..."
	const cleaned = dn.trim()

	// Попробуем comma-форму
	let cn = /(?:^|[,])\s*CN=([^,\/]+)/.exec(cleaned)?.[1]
	let o = /(?:^|[,])\s*O=([^,\/]+)/.exec(cleaned)?.[1]

	// Если не нашли — пробуем slash-форму
	if (!cn || !o) {
		cn = /\/CN=([^\/,]+)/.exec(cleaned)?.[1] ?? cn
		o = /\/O=([^\/,]+)/.exec(cleaned)?.[1] ?? o
	}
	return { cn, o }
}

function identityFromNginxHeaders(req: express.Request): ClientIdentity | null {
	const verify = req.get('X-SSL-Client-Verify')
	const subject = req.get('X-Client-Subject') ?? ''
	const issuer = req.get('X-Client-Issuer') ?? ''
	const hasCert = Boolean(req.get('X-Client-Cert'))

	// шумный, но полезный лог до парсинга
	console.warn('nginx mTLS headers', { verify, subject, issuer, hasCert })

	if (verify !== 'SUCCESS') {
		console.warn('nginx verify not SUCCESS:', verify)
		return null
	}

	const { cn, o } = parseDn(subject)
	console.warn('parsed DN', { cn, o })

	if (!cn) {
		console.warn('missing CN in subject DN')
		return null
	}
	if (cn.startsWith('subnet:')) {
		const subnetId = cn.slice('subnet:'.length)
		if (!subnetId) {
			console.warn('empty subnetId in CN')
			return null
		}
		return { type: 'hub', subnetId }
	}
	if (cn.startsWith('node:')) {
		const nodeId = cn.slice('node:'.length)
		const subnetId = o?.startsWith('subnet:')
			? o.slice('subnet:'.length)
			: undefined
		if (!nodeId || !subnetId) {
			console.warn('node identity missing nodeId or subnetId', {
				nodeId,
				subnetId,
				o,
			})
			return null
		}
		return { type: 'node', subnetId, nodeId }
	}

	console.warn('unsupported CN format', { cn })
	return null
}

mtlsRouter.use((req, res, next) => {
	const tlsSocket = req.socket as any // TLSSocket
	const isEncrypted = tlsSocket?.encrypted === true
	const authorized = tlsSocket?.authorized === true
	const authError = tlsSocket?.authorizationError
	const peer = isEncrypted ? tlsSocket.getPeerCertificate?.() ?? null : null

	console.warn('tls gate', {
		isEncrypted,
		authorized,
		authError,
		hasPeer: !!peer,
		peerSubject: peer?.subject,
		peerIssuer: peer?.issuer,
	})

	if (isEncrypted && authorized) {
		const id = getClientIdentity(req)
		if (id) {
			req.auth = id
			return next()
		}
	}

	const id2 = identityFromNginxHeaders(req)
	if (id2) {
		req.auth = id2
		return next()
	}

	return respondError(req, res, 401, 'client_certificate_required')
})

mtlsRouter.get('/policy', (_req, res) => {
	res.json(POLICY_RESPONSE)
})

mtlsRouter.post('/hub/nats/token', async (req, res) => {
	const identity = req.auth
	if (!identity || identity.type !== 'hub') {
		return respondError(req, res, 403, 'hub_certificate_required')
	}
	try {
		const hubId = identity.subnetId
		const session = await issueHubNatsSession(hubId)
		const ws_url = buildPublicNatsWsUrl()
		return res.json({
			ok: true,
			hub_id: hubId,
			hub_nats_token: session.token,
			hub_nats_token_expires_at: session.expiresAt,
			nats_ws_url: ws_url,
			nats_user: session.user,
		})
	} catch (error) {
		return handleError(req, res, error, {
			status: 500,
			code: 'internal_error',
		})
	}
})

// Hub developer endpoints: allow hubs to fetch Root logs without ROOT_TOKEN.
// NOTE: still unsafe in production; keep behind mTLS and use only for debugging.
mtlsRouter.get('/hub/dev/logs', async (req, res) => {
	if (process.env['DEBUG_ENDPOINTS'] !== 'true') {
		return res.status(404).json({ ok: false, error: 'debug_endpoints_disabled' })
	}
	const identity = req.auth
	if (!identity || identity.type !== 'hub') {
		return respondError(req, res, 403, 'hub_certificate_required')
	}
	const minutesRaw = Number(String(req.query['minutes'] ?? '30'))
	const minutes = Number.isFinite(minutesRaw)
		? Math.max(1, Math.min(12 * 60, Math.floor(minutesRaw)))
		: 30
	const limitRaw = Number(String(req.query['limit'] ?? '2000'))
	const limit = Number.isFinite(limitRaw)
		? Math.max(1, Math.min(50_000, Math.floor(limitRaw)))
		: 2000
	const contains =
		typeof req.query['contains'] === 'string'
			? String(req.query['contains'])
			: null
	const sinceMs = Date.now() - minutes * 60 * 1000
	// Default filter to the requesting hub id to reduce accidental leaks.
	const hubId = typeof req.query['hub_id'] === 'string'
		? String(req.query['hub_id'])
		: identity.subnetId
	const items = queryRootLogs({ sinceMs, limit, contains, hubId })
	return res.json({ ok: true, minutes, limit, since_ms: sinceMs, items })
})

mtlsRouter.get('/hub/dev/log_files', async (req, res) => {
	if (process.env['DEBUG_ENDPOINTS'] !== 'true') {
		return res.status(404).json({ ok: false, error: 'debug_endpoints_disabled' })
	}
	const identity = req.auth
	if (!identity || identity.type !== 'hub') {
		return respondError(req, res, 403, 'hub_certificate_required')
	}
	try {
		const contains =
			typeof req.query['contains'] === 'string'
				? String(req.query['contains'])
				: null
		const limitRaw = Number(String(req.query['limit'] ?? '200'))
		const limit = Number.isFinite(limitRaw) ? limitRaw : 200
		const items = await listLogFiles({ contains, limit })
		return res.json({ ok: true, items })
	} catch (e: any) {
		return res.status(500).json({ ok: false, error: String(e?.message ?? e) })
	}
})

mtlsRouter.get('/hub/dev/log_tail', async (req, res) => {
	if (process.env['DEBUG_ENDPOINTS'] !== 'true') {
		return res.status(404).json({ ok: false, error: 'debug_endpoints_disabled' })
	}
	const identity = req.auth
	if (!identity || identity.type !== 'hub') {
		return respondError(req, res, 403, 'hub_certificate_required')
	}
	try {
		const file = typeof req.query['file'] === 'string' ? String(req.query['file']) : ''
		const linesRaw = Number(String(req.query['lines'] ?? '200'))
		const lines = Number.isFinite(linesRaw) ? linesRaw : 200
		const maxBytesRaw = Number(String(req.query['max_bytes'] ?? '2000000'))
		const maxBytes = Number.isFinite(maxBytesRaw) ? maxBytesRaw : 2_000_000
		const result = await tailLogFile({ relPath: file, lines, maxBytes })
		return res.json({ ok: true, ...result })
	} catch (e: any) {
		return res.status(400).json({ ok: false, error: String(e?.message ?? e) })
	}
})

mtlsRouter.post('/hub/core_update/report', async (req, res) => {
	const identity = req.auth
	if (!identity || identity.type !== 'hub') {
		return respondError(req, res, 403, 'hub_certificate_required')
	}
	try {
		const body = typeof req.body === 'object' && req.body !== null ? req.body : {}
		const { streamId: incomingStreamId, messageId: incomingMessageId, cursor: incomingCursor } = protocolMetaFromRecord(
			body as Record<string, unknown>,
		)
		const existingRecord = parseStoredHubReport(
			await redisClient.hGet(ROOT_CORE_UPDATE_REPORTS_HASH, identity.subnetId),
		)
		const { streamId: existingStreamId, messageId: existingMessageId, cursor: existingCursor } =
			protocolMetaFromRecord(existingRecord)
		if (
			incomingStreamId &&
			existingStreamId &&
			incomingStreamId === existingStreamId &&
			incomingCursor !== null &&
			existingCursor !== null &&
			(existingCursor > incomingCursor ||
				(existingCursor === incomingCursor &&
					(existingMessageId === incomingMessageId || !!incomingMessageId)))
		) {
			return res.status(202).json({
				ok: true,
				hub_id: identity.subnetId,
				accepted: false,
				duplicate: true,
				stream_id: incomingStreamId,
				message_id: incomingMessageId || null,
				cursor: incomingCursor,
				stored_cursor: existingCursor,
			})
		}
		const record = {
			hub_id: identity.subnetId,
			root_received_at: new Date().toISOString(),
			root_ack_result: 'accepted',
			...body,
		}
		await redisClient.hSet(
			ROOT_CORE_UPDATE_REPORTS_HASH,
			identity.subnetId,
			JSON.stringify(record),
		)
		await redisClient.hSet(
			ROOT_CORE_UPDATE_SUBNETS_HASH,
			identity.subnetId,
			JSON.stringify(normalizeCoreUpdateSubnetState(record)),
		)
		return res.status(202).json({
			ok: true,
			hub_id: identity.subnetId,
			accepted: true,
			duplicate: false,
			stream_id: incomingStreamId || null,
			message_id: incomingMessageId || null,
			cursor: incomingCursor,
		})
	} catch (error) {
		return res.status(500).json({ ok: false, error: String((error as Error)?.message || error) })
	}
})

mtlsRouter.post('/hub/control/report', async (req, res) => {
	const identity = req.auth
	if (!identity || identity.type !== 'hub') {
		return respondError(req, res, 403, 'hub_certificate_required')
	}
	try {
		const body = typeof req.body === 'object' && req.body !== null ? req.body : {}
		const { streamId: incomingStreamId, messageId: incomingMessageId, cursor: incomingCursor } = protocolMetaFromRecord(
			body as Record<string, unknown>,
		)
		const existingRecord = parseStoredHubReport(
			await redisClient.hGet(ROOT_HUB_CONTROL_REPORTS_HASH, identity.subnetId),
		)
		const { streamId: existingStreamId, messageId: existingMessageId, cursor: existingCursor } =
			protocolMetaFromRecord(existingRecord)
		if (
			incomingStreamId &&
			existingStreamId &&
			incomingStreamId === existingStreamId &&
			incomingCursor !== null &&
			existingCursor !== null &&
			(existingCursor > incomingCursor ||
				(existingCursor === incomingCursor &&
					(existingMessageId === incomingMessageId || !!incomingMessageId)))
		) {
			return res.status(202).json({
				ok: true,
				hub_id: identity.subnetId,
				accepted: false,
				duplicate: true,
				stream_id: incomingStreamId,
				message_id: incomingMessageId || null,
				cursor: incomingCursor,
				stored_cursor: existingCursor,
			})
		}
		const record = {
			hub_id: identity.subnetId,
			root_received_at: new Date().toISOString(),
			root_ack_result: 'accepted',
			...body,
		}
		await redisClient.hSet(
			ROOT_HUB_CONTROL_REPORTS_HASH,
			identity.subnetId,
			JSON.stringify(record),
		)
		return res.status(202).json({
			ok: true,
			hub_id: identity.subnetId,
			accepted: true,
			duplicate: false,
			stream_id: incomingStreamId || null,
			message_id: incomingMessageId || null,
			cursor: incomingCursor,
		})
	} catch (error) {
		return res.status(500).json({ ok: false, error: String((error as Error)?.message || error) })
	}
})

mtlsRouter.get('/hub/core_update/release', async (req, res) => {
	const identity = req.auth
	if (!identity || identity.type !== 'hub') {
		return respondError(req, res, 403, 'hub_certificate_required')
	}
	try {
		const branch = typeof req.query['branch'] === 'string' ? String(req.query['branch']).trim() : ''
		const currentCommit = typeof req.query['current_commit'] === 'string' ? String(req.query['current_commit']).trim() : ''
		const subnetId = identity.subnetId
		let subnetState: Record<string, any> | null = null
		const rawSubnetState = await redisClient.hGet(ROOT_CORE_UPDATE_SUBNETS_HASH, subnetId)
		if (rawSubnetState) {
			try {
				subnetState = JSON.parse(rawSubnetState)
			} catch {
				subnetState = null
			}
		}
		const effectiveBranch = branch || String(subnetState?.current_branch || '').trim() || CORE_UPDATE_GITHUB_BRANCH
		let release: Record<string, any> | null = null
		if (effectiveBranch) {
			const rawRelease = await redisClient.hGet(ROOT_CORE_UPDATE_RELEASES_HASH, effectiveBranch)
			if (rawRelease) {
				try {
					release = JSON.parse(rawRelease)
				} catch {
					release = null
				}
			}
		}
		const releaseCommit = String(release?.head_sha || '').trim()
		const subnetCommit = currentCommit || String(subnetState?.current_commit || '').trim()
		const needsUpdate = Boolean(releaseCommit && releaseCommit !== subnetCommit)
		return res.json({
			ok: true,
			subnet_id: subnetId,
			branch: effectiveBranch,
			release,
			subnet_state: subnetState,
			needs_update: needsUpdate,
			reason: needsUpdate ? 'release_commit_mismatch' : 'up_to_date',
		})
	} catch (error) {
		return res.status(500).json({ ok: false, error: String((error as Error)?.message || error) })
	}
})

const createDraftHandler =
	(kind: DraftKind): express.RequestHandler =>
		async (req, res) => {
			const identity = req.auth
			if (!identity)
				return respondError(req, res, 401, 'client_certificate_required')

			// Разрешаем и node, и hub
			const name = typeof req.body?.name === 'string' ? req.body.name : ''
			const archiveB64 =
				typeof req.body?.archive_b64 === 'string'
					? req.body.archive_b64
					: ''
			const sha256 =
				typeof req.body?.sha256 === 'string' ? req.body.sha256 : undefined

			if (!name || !archiveB64)
				return respondError(req, res, 400, 'archive_fields_required')

			// поддержка node_id в payload, если пуш идет "от хаба от имени ноды" (опционально)
			const payloadNodeId =
				typeof req.body?.node_id === 'string' ? req.body.node_id : ''

			// Определяем целевой контекст хранения
			let subnetId: string
			let nodeId: string

			if (identity.type === 'node') {
				subnetId = identity.subnetId
				nodeId = identity.nodeId
				if (payloadNodeId && payloadNodeId !== nodeId) {
					return respondError(req, res, 403, 'node_mismatch')
				}
			} else if (identity.type === 'hub') {
				subnetId = identity.subnetId
				// режим «хаб пушит черновик на уровень подсети», без привязки к конкретной ноде:
				nodeId = payloadNodeId || 'hub'
			} else {
				return respondError(req, res, 403, 'invalid_client_certificate')
			}

			// дальше как было: decode, check size, verify SHA, writeDraft
			let archive: Buffer
			try {
				assertSafeName(name)
				archive = decodeArchive(archiveB64)
				if (!archive.length) throw new HttpError(400, 'archive_empty')
				if (archive.length > MAX_ARCHIVE_BYTES)
					return respondError(req, res, 413, 'archive_too_large')
				verifySha256(archive, sha256)
			} catch (error) {
				return handleError(req, res, error, {
					status: 400,
					code: 'invalid_archive',
				})
			}

			const started = Date.now()
			try {
				const result = await forgeManager.writeDraft({
					kind,
					subnetId,
					nodeId,
					name,
					archive,
				})
				const keyPrefix =
					kind === 'skills'
						? SKILL_FORGE_KEY_PREFIX
						: SCENARIO_FORGE_KEY_PREFIX
				await redisClient.set(
					`${keyPrefix}:${subnetId}:${nodeId}:${name}`,
					JSON.stringify({
						stored_path: result.storedPath,
						commit: result.commitSha,
						sha256: sha256 ?? null,
						ts: Date.now(),
					})
				)
				res.json({
					ok: true,
					stored_path: result.storedPath,
					commit: result.commitSha,
					sha256: sha256 ?? null,
				})
			} catch (error) {
				console.error('failed to store draft', error)
				handleError(req, res, error, {
					status: 500,
					code: 'draft_store_failed',
				})
			}
		}

const getDraftMetaHandler =
	(kind: DraftKind): express.RequestHandler =>
		async (req, res) => {
			const identity = req.auth
			if (!identity)
				return respondError(req, res, 401, 'client_certificate_required')

			const name = qstr(req.query, 'name')
			const payloadNodeId = qstr(req.query, 'node_id')
			if (!name) return respondError(req, res, 400, 'missing_params')
			try {
				assertSafeName(name)
			} catch {
				return respondError(req, res, 400, 'invalid_name')
			}

			let subnetId: string
			let nodeId: string

			if (identity.type === 'node') {
				subnetId = identity.subnetId
				nodeId = identity.nodeId
				if (payloadNodeId && payloadNodeId !== nodeId) {
					return respondError(req, res, 403, 'node_mismatch')
				}
			} else if (identity.type === 'hub') {
				subnetId = identity.subnetId
				nodeId = payloadNodeId || 'hub'
			} else {
				return respondError(req, res, 403, 'invalid_client_certificate')
			}

			const keyPrefix =
				kind === 'skills' ? SKILL_FORGE_KEY_PREFIX : SCENARIO_FORGE_KEY_PREFIX
			const key = `${keyPrefix}:${subnetId}:${nodeId}:${name}`
			try {
				const raw = await redisClient.get(key)
				if (!raw) return respondError(req, res, 404, 'not_found')
				let parsed: any = {}
				try {
					parsed = JSON.parse(raw)
				} catch {
					parsed = {}
				}
				return res.json({
					ok: true,
					name,
					subnet_id: subnetId,
					node_id: nodeId,
					stored_path: parsed?.stored_path ?? null,
					commit: parsed?.commit ?? null,
					sha256: parsed?.sha256 ?? null,
					ts: parsed?.ts ?? null,
				})
			} catch (error) {
				return handleError(req, res, error, {
					status: 500,
					code: 'draft_meta_failed',
				})
			}
		}

const getDraftArchiveHandler =
	(kind: DraftKind): express.RequestHandler =>
		async (req, res) => {
			const identity = req.auth
			if (!identity)
				return respondError(req, res, 401, 'client_certificate_required')

			const name = qstr(req.query, 'name')
			const payloadNodeId = qstr(req.query, 'node_id')
			if (!name) return respondError(req, res, 400, 'missing_params')
			try {
				assertSafeName(name)
			} catch {
				return respondError(req, res, 400, 'invalid_name')
			}

			let subnetId: string
			let nodeId: string

			if (identity.type === 'node') {
				subnetId = identity.subnetId
				nodeId = identity.nodeId
				if (payloadNodeId && payloadNodeId !== nodeId) {
					return respondError(req, res, 403, 'node_mismatch')
				}
			} else if (identity.type === 'hub') {
				subnetId = identity.subnetId
				nodeId = payloadNodeId || 'hub'
			} else {
				return respondError(req, res, 403, 'invalid_client_certificate')
			}

			const keyPrefix =
				kind === 'skills' ? SKILL_FORGE_KEY_PREFIX : SCENARIO_FORGE_KEY_PREFIX
			const key = `${keyPrefix}:${subnetId}:${nodeId}:${name}`
			try {
				const raw = await redisClient.get(key)
				if (!raw) return respondError(req, res, 404, 'not_found')
				let parsed: any = {}
				try {
					parsed = JSON.parse(raw)
				} catch {
					parsed = {}
				}
				const storedPath =
					typeof parsed?.stored_path === 'string' ? parsed.stored_path : ''
				if (!storedPath) return respondError(req, res, 404, 'not_found')
				const abs = path.resolve(forgeManagerWorkdir(), storedPath)
				if (!fs.existsSync(abs)) return respondError(req, res, 404, 'not_found')
				const zip = new AdmZip()
				zip.addLocalFolder(abs)
				const buffer = zip.toBuffer()
				const b64 = buffer.toString('base64')
				return res.json({
					ok: true,
					name,
					archive_b64: b64,
					sha256: parsed?.sha256 ?? null,
				})
			} catch (error) {
				return handleError(req, res, error, {
					status: 500,
					code: 'draft_archive_failed',
				})
			}
		}

function forgeManagerWorkdir(): string {
	// Keep in sync with ForgeManager initialization above.
	return FORGE_WORKDIR || '/var/lib/adaos/forge'
}

const qstr = (q: any, key: string): string =>
	typeof q?.[key] === 'string' ? q[key] : ''
const qbool = (q: any, key: string): boolean => {
	const v = q?.[key]
	return v === true || v === 'true' || v === '1'
}

const deleteDraftHandler =
	(kind: DraftKind): express.RequestHandler =>
		async (req, res) => {
			const identity = req.auth
			if (!identity)
				return respondError(req, res, 401, 'client_certificate_required')

			const name = qstr(req.query, 'name')
			const payloadNodeId = qstr(req.query, 'node_id')
			const allNodes = qbool(req.query, 'all_nodes')
			if (!name) return respondError(req, res, 400, 'missing_params')

			try {
				assertSafeName(name)
			} catch {
				return respondError(req, res, 400, 'invalid_name')
			}

			let subnetId: string
			let nodeId: string | undefined

			if (identity.type === 'node') {
				subnetId = identity.subnetId
				nodeId = identity.nodeId
				if ((payloadNodeId && payloadNodeId !== nodeId) || allNodes) {
					return respondError(req, res, 403, 'node_mismatch')
				}
			} else if (identity.type === 'hub') {
				subnetId = identity.subnetId
				nodeId = payloadNodeId || undefined
			} else {
				return respondError(req, res, 403, 'invalid_client_certificate')
			}

			const started = Date.now()
			try {
				const result = await forgeManager.deleteDraft({
					kind,
					subnetId,
					name,
					nodeId,
					allNodes,
				})

				const keyPrefix =
					kind === 'skills'
						? SKILL_FORGE_KEY_PREFIX
						: SCENARIO_FORGE_KEY_PREFIX
				const keysToDelete = result.redisKeys ?? []
				if (keysToDelete.length) {
					await redisClient.del(keysToDelete)
				}

				console.info('draft deleted', {
					action: 'delete_draft',
					kind,
					subnetId,
					nodeId,
					name,
					allNodes,
					duration_ms: Date.now() - started,
					auditId: result.auditId,
					deleted: result.deleted,
				})

				if (!result.deleted.length) {
					return res.status(204).end()
				}

				return res.json({
					ok: true,
					deleted: result.deleted,
					audit_id: result.auditId,
				})
			} catch (error: any) {
				if (error?.code === 'not_found') {
					return respondError(req, res, 404, 'not_found')
				}
				if (error?.code === 'invalid_name') {
					return respondError(req, res, 400, 'invalid_name')
				}
				console.error('failed to delete draft', error)
				return handleError(req, res, error, {
					status: 500,
					code: 'draft_delete_failed',
				})
			}
		}

const deleteRegistryHandler =
	(kind: DraftKind): express.RequestHandler =>
		async (req, res) => {
			const identity = req.auth
			if (!identity)
				return respondError(req, res, 401, 'client_certificate_required')

			const name = qstr(req.query, 'name')
			const version = qstr(req.query, 'version') || undefined
			const allVersions = qbool(req.query, 'all_versions')
			const force = qbool(req.query, 'force')

			if (!name) return respondError(req, res, 400, 'missing_params')
			if (!version && !allVersions)
				return respondError(req, res, 400, 'missing_params')

			try {
				assertSafeName(name)
				if (version) assertSafeName(version)
			} catch {
				return respondError(req, res, 400, 'invalid_name')
			}

			const subnetId = identity.subnetId

			const started = Date.now()
			try {
				const result = await forgeManager.deleteRegistry({
					kind,
					subnetId,
					name,
					version,
					allVersions,
					force,
				})

				console.info('registry artifact deleted', {
					action: 'delete_registry',
					kind,
					subnetId,
					name,
					version,
					allVersions,
					force,
					duration_ms: Date.now() - started,
					auditId: result.auditId,
					deleted: result.deleted,
					skipped: result.skipped ?? [],
					tombstoned: !!result.tombstoned,
				})

				if (result.deleted?.length) {
					return res.json({
						ok: true,
						deleted: result.deleted,
						skipped: result.skipped ?? [],
						audit_id: result.auditId,
						tombstoned: !!result.tombstoned,
					})
				}

				return res.status(204).end()
			} catch (error: any) {
				if (error?.code === 'not_found') {
					return respondError(req, res, 404, 'not_found')
				}
				if (error?.code === 'in_use') {
					return respondError(req, res, 409, 'in_use', {
						refs: error.refs || [],
					})
				}
				console.error('failed to delete registry artifact', error)
				return handleError(req, res, error, {
					status: 500,
					code: 'registry_delete_failed',
				})
			}
		}

mtlsRouter.post('/skills/draft', createDraftHandler('skills'))
mtlsRouter.post('/scenarios/draft', createDraftHandler('scenarios'))

mtlsRouter.get('/skills/draft', getDraftMetaHandler('skills'))
mtlsRouter.get('/scenarios/draft', getDraftMetaHandler('scenarios'))

mtlsRouter.get('/skills/draft/archive', getDraftArchiveHandler('skills'))
mtlsRouter.get('/scenarios/draft/archive', getDraftArchiveHandler('scenarios'))

mtlsRouter.delete('/skills/draft', deleteDraftHandler('skills'))
mtlsRouter.delete('/scenarios/draft', deleteDraftHandler('scenarios'))

mtlsRouter.delete('/skills/registry', deleteRegistryHandler('skills'))
mtlsRouter.delete('/scenarios/registry', deleteRegistryHandler('scenarios'))

mtlsRouter.post('/skills/pr', (req, res) => {
	const identity = req.auth
	if (!identity || identity.type !== 'hub') {
		respondError(req, res, 403, 'hub_certificate_required')
		return
	}
	const name = typeof req.body?.name === 'string' ? req.body.name : ''
	const nodeId = typeof req.body?.node_id === 'string' ? req.body.node_id : ''
	if (!name || !nodeId) {
		respondError(req, res, 400, 'name_and_node_required')
		return
	}
	res.json({
		ok: true,
		pr_url: 'https://github.com/stipot-com/adaos-registry/pull/mock-skill',
	})
})

mtlsRouter.post('/scenarios/pr', (req, res) => {
	const identity = req.auth
	if (!identity || identity.type !== 'hub') {
		respondError(req, res, 403, 'hub_certificate_required')
		return
	}
	const name = typeof req.body?.name === 'string' ? req.body.name : ''
	const nodeId = typeof req.body?.node_id === 'string' ? req.body.node_id : ''
	if (!name || !nodeId) {
		respondError(req, res, 400, 'name_and_node_required')
		return
	}
	res.json({
		ok: true,
		pr_url: 'https://github.com/stipot-com/adaos-registry/pull/mock-scenario',
	})
})

app.use('/v1', mtlsRouter)

function isValidGuid(guid: string) {
	return /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(
		guid
	)
}

const openedStreams: OpenedStreams = {}
const FILESPATH = '/tmp/inimatic_public_files/'

if (!fs.existsSync(FILESPATH)) {
	fs.mkdirSync(FILESPATH)
}

function cleanupSessionBucket(sessionId: string) {
	if (
		openedStreams[sessionId] &&
		!Object.keys(openedStreams[sessionId]).length
	) {
		delete openedStreams[sessionId]
	}
}

const safeBasename = (value: string) => path.basename(value)

function saveFileChunk(
	sessionId: string,
	fileName: string,
	content: Array<number>
) {
	if (!openedStreams[sessionId]) {
		openedStreams[sessionId] = {}
	}

	const safeFile = safeBasename(fileName)

	if (!openedStreams[sessionId][safeFile]) {
		const timestamp = String(Date.now())
		const stream = fs.createWriteStream(
			FILESPATH + timestamp + '_' + safeFile
		)
		const destroyTimeout = setTimeout(() => {
			openedStreams[sessionId][safeFile].stream.destroy()
			fs.unlink(
				FILESPATH +
				openedStreams[sessionId][safeFile].timestamp +
				'_' +
				safeFile,
				() => { }
			)
			delete openedStreams[sessionId][safeFile]
			cleanupSessionBucket(sessionId)
			console.log('destroy', openedStreams)
		}, 30000)

		openedStreams[sessionId][safeFile] = {
			stream,
			destroyTimeout,
			timestamp,
		}
	}

	clearTimeout(openedStreams[sessionId][safeFile].destroyTimeout)
	openedStreams[sessionId][safeFile].destroyTimeout = setTimeout(() => {
		openedStreams[sessionId][safeFile].stream.destroy()
		fs.unlink(
			FILESPATH +
			openedStreams[sessionId][safeFile].timestamp +
			'_' +
			safeFile,
			(error) => {
				if (error) console.log(error)
			}
		)
		delete openedStreams[sessionId][safeFile]
		cleanupSessionBucket(sessionId)
		console.log('destroy', openedStreams)
	}, 30000)

	return new Promise<void>((resolve, reject) =>
		openedStreams[sessionId][safeFile].stream.write(
			new Uint8Array(content),
			(error) => (error ? reject(error) : resolve())
		)
	)
}

const registerSocketHandlers = (socket: Socket) => {
	const namespace = socket.nsp
	console.log(socket.id)

	if (namespace.name === SOCKET_CHANNEL_NS) {
		socket.emit('channel_version', SOCKET_CHANNEL_VERSION)
	}

	socket.on('disconnecting', async () => {
		const rooms = Array.from(socket.rooms).filter(
			(roomId) => roomId != socket.id
		)
		if (!rooms.length) return

		const sessionId = rooms[0]
		console.log('disconnect', socket.id, socket.rooms, sessionId)
		const sessionData: UnionSessionData = JSON.parse(
			(await redisClient.get(sessionId))!
		)

		if (sessionData == null) {
			socket.to(sessionId).emit('initiator_disconnect')
			return
		}

		const isInitiator = sessionData.initiatorSocketId === socket.id
		if (isInitiator) {
			if (sessionData.type === 'public') {
				await Promise.all(
					sessionData.fileNames.map((item) => {
						const filePath =
							FILESPATH + item.timestamp + '_' + item.fileName

						return new Promise<void>((resolve) =>
							fs.unlink(filePath, () => resolve())
						)
					})
				)
			}

			socket.to(sessionId).emit('initiator_disconnect')
			namespace.socketsLeave(sessionId)
			await redisClient.del(sessionId)
		} else {
			namespace
				.to(sessionData.initiatorSocketId)
				.emit('follower_disconnect', sessionData.followers[socket.id])

			delete sessionData.followers[socket.id]
			await redisClient.set(sessionId, JSON.stringify(sessionData))
		}
	})

	socket.on('add_initiator', async (type) => {
		const guid = uuidv4()
		let sessionData: UnionSessionData
		if (type === 'private') {
			sessionData = {
				initiatorSocketId: socket.id,
				followers: {},
				timestamp: new Date(),
				type: type,
			}
		} else {
			sessionData = {
				initiatorSocketId: socket.id,
				followers: {},
				timestamp: new Date(),
				type: type,
				fileNames: [],
			}
		}

		await redisClient.set(guid, JSON.stringify(sessionData))
		await redisClient.expire(guid, 3600)

		socket.join(guid)
		socket.emit('session_id', guid)
	})

	async function sendToPublicFollower(targetSocket: Socket, emitObject: any) {
		return new Promise<void>((resolve) => {
			targetSocket.emit('communication', emitObject, () => resolve())
		})
	}

	async function distributeSessionFiles(
		targetSocket: Socket,
		fileNames: Array<{ fileName: string; timestamp: string }>
	) {
		const chunksize = 64 * 1024

		for (const item of fileNames) {
			const filePath = FILESPATH + item.timestamp + '_' + item.fileName
			const readStream = fs.createReadStream(filePath, {
				highWaterMark: chunksize,
			})

			const size = (await stat(filePath)).size

			await sendToPublicFollower(targetSocket, {
				type: 'transferFile',
				fileName: item.fileName,
				size,
			})

			for await (const chunk of readStream) {
				await sendToPublicFollower(targetSocket, {
					type: 'writeFile',
					fileName: item.fileName,
					content: Array.from(new Uint8Array(chunk as Buffer)),
					size,
				})
			}

			await sendToPublicFollower(targetSocket, {
				type: 'fileTransfered',
				fileName: item.fileName,
			})
		}
	}

	socket.on(
		'set_session_data',
		async (sessionId: string, sessionData: SessionData) => {
			if (!isValidGuid(sessionId)) {
				console.error('sessionId must be in guid format')
				return
			}

			await redisClient.set(sessionId, JSON.stringify(sessionData))
			await redisClient.expire(sessionId, 3600)
		}
	)

	socket.on(
		'join_session',
		async ({ followerName, sessionId }: FollowerData) => {
			if (!isValidGuid(sessionId)) {
				console.error('sessionId must be in guid format')
				return
			}

			let sessionData: UnionSessionData | null
			try {
				const sessionString = await redisClient.get(sessionId)
				sessionData = sessionString ? JSON.parse(sessionString) : null
			} catch (error) {
				console.error(error)
				return
			}

			if (sessionData == null) {
				socket.emit('session_unavailable')
				return
			}

			if (sessionData.followers[socket.id]) {
				return
			}

			if (sessionData.type === 'public') {
				socket.emit('session_type', 'public')
				await socket.join(sessionId)
				sessionData.followers[socket.id] = followerName
				await redisClient.set(sessionId, JSON.stringify(sessionData))
				await distributeSessionFiles(socket, sessionData.fileNames)
				return
			}

			socket.join(sessionId)
			sessionData.followers[socket.id] = followerName
			await redisClient.set(sessionId, JSON.stringify(sessionData))

			socket.emit('session_type', 'private')
			namespace
				.to(sessionData.initiatorSocketId)
				.emit('follower_connect', followerName)
		}
	)

	socket.on('set_session_public_files', async ({ sessionId, fileNames }) => {
		const sessionString = await redisClient.get(sessionId)
		const sessionData: PublicSessionData = sessionString
			? JSON.parse(sessionString)
			: null

		if (sessionData == null) {
			socket.emit('session_unavailable')
			return
		}

		sessionData.fileNames = fileNames
		await redisClient.set(sessionId, JSON.stringify(sessionData))
	})

	socket.on(
		'communication',
		async ({ isInitiator, sessionId, data }: CommunicationData) => {
			if (!isValidGuid(sessionId)) {
				console.error('sessionId must be in guid format')
				return
			}

			if (isInitiator) {
				const firstValue = data['values'][0]

				if (firstValue['type'] === 'transferFile') {
					const safeFileName = safeBasename(firstValue['fileName'])
					const pathToFile =
						FILESPATH + firstValue['timestamp'] + '_' + safeFileName
					await new Promise<void>((resolve) =>
						fs.unlink(pathToFile, () => resolve())
					)
					delete openedStreams[sessionId][safeFileName]
					cleanupSessionBucket(sessionId)
				}

				namespace.to(sessionId).emit('communication', data)
				return
			}

			const sessionData = (await redisClient.get(sessionId))!
			const initiatorSocketId = (JSON.parse(sessionData) as SessionData)
				.initiatorSocketId

			const messageType = data['type']

			if (messageType === 'writeFile') {
				await saveFileChunk(
					sessionId,
					data['fileName'],
					data['content']
				)
			}

			namespace.to(initiatorSocketId).emit('communication', data)
		}
	)
}

io.on('connection', registerSocketHandlers)

const nsv1 = io.of(SOCKET_CHANNEL_NS)
nsv1.use((socket, next) => next())
nsv1.on('connection', (socket) => registerSocketHandlers(socket))

function closeStreams() {
	for (const sessionId of Object.keys(openedStreams)) {
		for (const info of Object.values(openedStreams[sessionId])) {
			try {
				info.stream.close()
			} catch {
				info.stream.destroy()
			}
		}
		delete openedStreams[sessionId]
	}
}

const shutdown = async () => {
	closeStreams()
	try {
		await redisClient.quit()
	} catch (error) {
		console.error('Failed to close redis client', error)
	}
	server.close(() => process.exit(0))
}

process.on('SIGINT', shutdown)
process.on('SIGTERM', shutdown)

server.listen(PORT, HOST, () => {
	const proto = USE_HTTP_SERVER ? 'http' : 'https'
	console.log(`AdaOS backhand listening on ${proto}://${HOST}:${PORT}`)
})
