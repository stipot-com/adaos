import 'dotenv/config'
import express from 'express'
import https from 'https'
import path from 'path'
import type { IncomingMessage } from 'http'
import { v4 as uuidv4 } from 'uuid'
import { Server, Socket } from 'socket.io'
import { createClient } from 'redis'
import fs from 'node:fs'
import { mkdir, stat, writeFile } from 'node:fs/promises'
import { randomBytes, createHash } from 'node:crypto'
import type { PeerCertificate, TLSSocket } from 'node:tls'
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { installAdaosBridge } from './adaos-bridge.js'
import { CertificateAuthority } from './pki.js'
import { ForgeManager, type DraftKind } from './forge.js'
import { getPolicy } from './policy.js'
import { resolveLocale, translate, type Locale, type MessageParams } from './i18n.js'
import { buildInfo } from './build-info.js'

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

class HttpError extends Error {
        status: number
        code: string
        params?: MessageParams

        constructor(status: number, code: string, params?: MessageParams, message?: string) {
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
        params?: MessageParams,
): void {
        const locale = req.locale ?? resolveLocale(req)
        const message = translate(locale, `errors.${code}`, params)
        res.status(status).json({ error: code, message })
}

function handleError(
        req: express.Request,
        res: express.Response,
        error: unknown,
        fallback?: { status?: number; code?: string; params?: MessageParams },
): void {
        if (error instanceof HttpError) {
                respondError(req, res, error.status, error.code, error.params)
                return
        }
        console.error('unexpected backend error', error)
        if (fallback) {
                respondError(req, res, fallback.status ?? 500, fallback.code ?? 'internal_error', fallback.params)
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
	const v = process.env[valName];
	if (v && v.includes('-----BEGIN')) {
		// поддержка варианта с \n в строке, если вдруг останется
		return v.replace(/\\n/g, '\n').trim() + '\n';
	}
	const f = process.env[fileName];
	if (f) {
		const text = readFileSync(resolve(f), 'utf8');
		return text.trim() + '\n';
	}
	throw new Error(`Environment variable ${valName} is required (or set ${fileName})`);
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

const HOST = process.env['HOST'] ?? '0.0.0.0'
const PORT = Number.parseInt(process.env['PORT'] ?? '3030', 10)
const ROOT_TOKEN = process.env['ROOT_TOKEN'] ?? 'dev-root-token'
const CA_KEY_PEM = readPemFromEnvOrFile('CA_KEY_PEM', 'CA_KEY_PEM_FILE');
const CA_CERT_PEM = readPemFromEnvOrFile('CA_CERT_PEM', 'CA_CERT_PEM_FILE');
/* TODO
const TLS_KEY_PEM  = readPemFromEnvOrFile('TLS_KEY_PEM',  'TLS_KEY_PEM_FILE');
const TLS_CERT_PEM = readPemFromEnvOrFile('TLS_CERT_PEM', 'TLS_CERT_PEM_FILE');
*/
const TLS_KEY_PEM = normalizePem(process.env['TLS_KEY_PEM'] ?? CA_KEY_PEM)
const TLS_CERT_PEM = normalizePem(process.env['TLS_CERT_PEM'] ?? CA_CERT_PEM)
const FORGE_GIT_URL = requireEnv('FORGE_GIT_URL')
const FORGE_SSH_KEY = process.env['FORGE_SSH_KEY']
const FORGE_AUTHOR_NAME = process.env['FORGE_GIT_AUTHOR_NAME'] ?? 'AdaOS Root'
const FORGE_AUTHOR_EMAIL = process.env['FORGE_GIT_AUTHOR_EMAIL'] ?? 'root@inimatic.local'
const FORGE_WORKDIR = process.env['FORGE_WORKDIR']
const SKILL_FORGE_KEY_PREFIX = 'forge:skills'
const SCENARIO_FORGE_KEY_PREFIX = 'forge:scenarios'
const BOOTSTRAP_TOKEN_TTL_SECONDS = 600

const policy = getPolicy()
const MAX_ARCHIVE_BYTES = policy.max_archive_mb * 1024 * 1024

const app = express()
app.use((req, _res, next) => {
        req.locale = resolveLocale(req)
        next()
})
app.use(express.json({ limit: '64mb' }))

function withLeadingSlash(value: string, fallback: string): string {
        const trimmed = value.trim()
        if (!trimmed) {
                return fallback
        }
        return trimmed.startsWith('/') ? trimmed : `/${trimmed}`
}

const SOCKET_PATH = withLeadingSlash(process.env['SOCKET_PATH'] ?? '/socket.io', '/socket.io')
const SOCKET_CHANNEL_NS = withLeadingSlash(process.env['SOCKET_CHANNEL_NS'] ?? '/adaos', '/adaos')
const SOCKET_CHANNEL_VERSION = (process.env['SOCKET_CHANNEL_VERSION'] ?? 'v1').trim() || 'v1'
const SOCKET_LEGACY_FALLBACK_ENABLED = (process.env['SOCKET_LEGACY_FALLBACK'] ?? '1') !== '0'

const server = https.createServer(
	{
		key: TLS_KEY_PEM,
		cert: TLS_CERT_PEM,
		ca: [CA_CERT_PEM],
		requestCert: true,
		rejectUnauthorized: false,
	},
	app,
)

const io = new Server(server, {
        cors: { origin: '*' },
        pingTimeout: 10000,
        pingInterval: 10000,
        path: SOCKET_PATH,
})

function extractHandshakeToken(req: IncomingMessage, searchParams: URLSearchParams): string | undefined {
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

        for (const candidate of ['token', 'auth[token]', 'authToken', 'auth_token']) {
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

io.engine.allowRequest = (req, callback) => {
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

const redisUrl = `redis://${process.env['PRODUCTION'] ? 'redis' : 'localhost'}:6379`
const redisClient = await createClient({ url: redisUrl })
	.on('error', (err) => console.error('Redis Client Error', err))
	.connect()

const certificateAuthority = new CertificateAuthority({ certPem: CA_CERT_PEM, keyPem: CA_KEY_PEM })
const forgeManager = new ForgeManager({
        repoUrl: FORGE_GIT_URL,
        workdir: FORGE_WORKDIR,
        authorName: FORGE_AUTHOR_NAME,
        authorEmail: FORGE_AUTHOR_EMAIL,
        sshKeyPath: FORGE_SSH_KEY,
})
await forgeManager.ensureReady()

const POLICY_RESPONSE = policy

const owners = new Map<string, OwnerRecord>()
const accessIndex = new Map<string, string>()
const refreshIndex = new Map<string, string>()
const deviceAuthorizations = new Map<string, DeviceAuthorization>()

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

function issueAccessToken(owner: OwnerRecord, scopes?: string[], subject?: string | null): { token: string; expiresAt: Date } {
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

function authenticateOwnerBearer(req: express.Request, res: express.Response, next: express.NextFunction) {
        const header = req.header('Authorization') ?? ''
        const token = header.startsWith('Bearer ') ? header.slice('Bearer '.length).trim() : ''
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
        if (owner.accessToken !== token || owner.accessExpiresAt.getTime() <= Date.now()) {
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
        if (!name || name.includes('..') || name.includes('/') || name.includes('\\')) {
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
        const actual = createHash('sha256').update(buffer).digest('hex').toLowerCase()
        if (normalized !== actual) {
                throw new HttpError(400, 'sha256_mismatch')
        }
}

async function useBootstrapToken(token: string): Promise<Record<string, unknown>> {
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
	const subjectRecord = subject as unknown as Partial<Record<string, string>> | undefined
	const cn = subjectRecord?.['CN'] ?? subjectRecord?.['cn']
	if (!cn) {
		return null
	}
	if (cn.startsWith('subnet:')) {
		const subnetId = cn.slice('subnet:'.length)
		if (!subnetId) {
			return null
		}
		return { type: 'hub', subnetId }
	}
	if (cn.startsWith('node:')) {
		const nodeId = cn.slice('node:'.length)
		const org = subjectRecord?.['O'] ?? subjectRecord?.['o']
		if (!nodeId || !org || !org.startsWith('subnet:')) {
			return null
		}
		const subnetId = org.slice('subnet:'.length)
		if (!subnetId) {
			return null
		}
		return { type: 'node', subnetId, nodeId }
	}
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
	res.status(200).type('text/plain').send('ok');
});

app.get('/healthz', (_req, res) => {
        res.json({
                ok: true,
                version: buildInfo.version,
                build_date: buildInfo.buildDate,
                commit: buildInfo.commit,
                time: new Date().toISOString(),
                mtls: true,
        })
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

const rootRouter = express.Router()

rootRouter.post('/auth/owner/start', (req, res) => {
        let ownerId: string
        try {
                ownerId = requireJsonField(req.body, 'owner_id')
        } catch (error) {
                handleError(req, res, error, { status: 400, code: 'missing_field', params: { field: 'owner_id' } })
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
        setTimeout(() => {
                const current = deviceAuthorizations.get(deviceCode)
                if (current) {
                        current.approved = true
                }
        }, 1000).unref()
        res.json({
                device_code: deviceCode,
                user_code: userCode,
                verify_uri: 'https://api.inimatic.com/device',
                interval: record.interval,
                expires_in: Math.floor((expiresAt.getTime() - Date.now()) / 1000),
        })
})

rootRouter.post('/auth/owner/poll', (req, res) => {
        let deviceCode: string
        try {
                deviceCode = requireJsonField(req.body, 'device_code')
        } catch (error) {
                handleError(req, res, error, { status: 400, code: 'missing_field', params: { field: 'device_code' } })
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
                handleError(req, res, error, { status: 400, code: 'missing_field', params: { field: 'refresh_token' } })
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
                handleError(req, res, error, { status: 400, code: 'missing_field', params: { field: 'hub_id' } })
                return
        }
        let hub = owner.hubs.get(hubId)
        if (!hub) {
                hub = { hubId, ownerId: owner.ownerId, createdAt: new Date(), lastSeen: new Date(), revoked: false }
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
        const ttlDays = parseTtl((req.body as Record<string, unknown> | undefined)?.['ttl'])
        const hub = owner.hubs.get(hubId)
        if (!hub || hub.revoked) {
                respondError(req, res, 404, 'hub_not_registered')
                return
        }
        hub.lastSeen = new Date()
        try {
                const result = certificateAuthority.issueClientCertificate({
                        csrPem,
                        subject: { commonName: `hub:${hubId}`, organizationName: `owner:${owner.ownerId}` },
                        validityDays: ttlDays,
                })
                res.json({ cert_pem: result.certificatePem, chain_pem: CA_CERT_PEM })
        } catch (error) {
                console.error('pki enrollment failed', error)
                handleError(req, res, error, { status: 400, code: 'certificate_issue_failed' })
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
                const hubs: Array<{ hub_id: string; owner_id: string; created_at: string; last_seen: string; key_fp: string }> = []
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

app.post('/v1/bootstrap_token', async (req, res) => {
        const token = req.header('X-Root-Token') ?? ''
        if (!token || token !== ROOT_TOKEN) {
                respondError(req, res, 401, 'unauthorized')
                return
        }
	const meta = (typeof req.body === 'object' && req.body !== null ? req.body : {}) as Record<string, unknown>
	const oneTimeToken = randomBytes(24).toString('hex')
	const expiresAt = new Date(Date.now() + BOOTSTRAP_TOKEN_TTL_SECONDS * 1000)
	await redisClient.setEx(
		`bootstrap:${oneTimeToken}`,
		BOOTSTRAP_TOKEN_TTL_SECONDS,
		JSON.stringify({ issued_at: new Date().toISOString(), ...meta }),
	)
	res.status(201).json({ one_time_token: oneTimeToken, expires_at: expiresAt.toISOString() })
})

app.post('/v1/subnets/register', async (req, res) => {
        const bootstrapToken = req.header('X-Bootstrap-Token') ?? ''
        if (!bootstrapToken) {
                respondError(req, res, 401, 'bootstrap_token_required')
                return
        }
        try {
                await useBootstrapToken(bootstrapToken)
        } catch (error) {
                handleError(req, res, error, { status: 401, code: 'invalid_bootstrap_token' })
                return
        }

        const csrPem = typeof req.body?.csr_pem === 'string' ? req.body.csr_pem : null
        if (!csrPem) {
                respondError(req, res, 400, 'bootstrap_csr_required')
                return
        }
	const subnetName = typeof req.body?.subnet_name === 'string' ? req.body.subnet_name : undefined

	const subnetId = generateSubnetId()
	let certPem: string
        try {
                const result = certificateAuthority.issueClientCertificate({
                        csrPem,
                        subject: { commonName: `subnet:${subnetId}` },
                })
                certPem = result.certificatePem
        } catch (error) {
                console.error('subnet certificate issue failed', error)
                handleError(req, res, error, { status: 400, code: 'certificate_issue_failed' })
                return
        }

	await forgeManager.ensureSubnet(subnetId)
	await redisClient.hSet(
		'root:subnets',
		subnetId,
		JSON.stringify({ subnet_id: subnetId, subnet_name: subnetName, created_at: Date.now() }),
	)

	res.status(201).json({
		subnet_id: subnetId,
		cert_pem: certPem,
		ca_pem: CA_CERT_PEM,
		forge: {
			repo: FORGE_GIT_URL,
			path: `subnets/${subnetId}`,
		},
	})
})

app.post('/v1/nodes/register', async (req, res) => {
        const bootstrapToken = req.header('X-Bootstrap-Token') ?? ''
        let subnetId: string | undefined

        if (bootstrapToken) {
                try {
                        await useBootstrapToken(bootstrapToken)
                } catch (error) {
                        handleError(req, res, error, { status: 401, code: 'invalid_bootstrap_token' })
                        return
                }
                const bodySubnet = typeof req.body?.subnet_id === 'string' ? req.body.subnet_id : undefined
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
                const bodySubnet = typeof req.body?.subnet_id === 'string' ? req.body.subnet_id : undefined
                if (bodySubnet && bodySubnet !== subnetId) {
                        respondError(req, res, 400, 'subnet_certificate_mismatch')
                        return
                }
        }

        if (!subnetId) {
                respondError(req, res, 400, 'subnet_required')
                return
        }

        const csrPem = typeof req.body?.csr_pem === 'string' ? req.body.csr_pem : null
        if (!csrPem) {
                respondError(req, res, 400, 'csr_required')
                return
        }

	const nodeId = generateNodeId()
	let certPem: string
        try {
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
                handleError(req, res, error, { status: 400, code: 'certificate_issue_failed' })
                return
        }

	await forgeManager.ensureSubnet(subnetId)
	await forgeManager.ensureNode(subnetId, nodeId)
	await redisClient.hSet(
		'root:nodes',
		nodeId,
		JSON.stringify({ node_id: nodeId, subnet_id: subnetId, created_at: Date.now() }),
	)

	res.status(201).json({ node_id: nodeId, subnet_id: subnetId, cert_pem: certPem, ca_pem: CA_CERT_PEM })
})

const mtlsRouter = express.Router()

mtlsRouter.use((req, res, next) => {
        const tlsSocket = req.socket as TLSSocket
        if (!tlsSocket.authorized) {
                respondError(req, res, 401, 'client_certificate_required')
                return
        }
        const identity = getClientIdentity(req)
        if (!identity) {
                respondError(req, res, 403, 'invalid_client_certificate')
                return
        }
        req.auth = identity
        next()
})

mtlsRouter.get('/policy', (_req, res) => {
	res.json(POLICY_RESPONSE)
})

const createDraftHandler = (kind: DraftKind): express.RequestHandler => async (req, res) => {
        const identity = req.auth
        if (!identity || identity.type !== 'node') {
                respondError(req, res, 403, 'node_certificate_required')
                return
        }

        const nodeId = typeof req.body?.node_id === 'string' ? req.body.node_id : ''
        if (!nodeId || nodeId !== identity.nodeId) {
                respondError(req, res, 403, 'node_mismatch')
                return
        }
        const name = typeof req.body?.name === 'string' ? req.body.name : ''
        const archiveB64 = typeof req.body?.archive_b64 === 'string' ? req.body.archive_b64 : ''
        const sha256 = typeof req.body?.sha256 === 'string' ? req.body.sha256 : undefined
        if (!name || !archiveB64) {
                respondError(req, res, 400, 'archive_fields_required')
                return
        }

        let archive: Buffer
        try {
                assertSafeName(name)
                archive = decodeArchive(archiveB64)
                if (!archive.length) {
                        throw new HttpError(400, 'archive_empty')
                }
                if (archive.length > MAX_ARCHIVE_BYTES) {
                        respondError(req, res, 413, 'archive_too_large')
                        return
                }
                verifySha256(archive, sha256)
        } catch (error) {
                handleError(req, res, error, { status: 400, code: 'invalid_archive' })
                return
        }

	const started = Date.now()
	try {
		const result = await forgeManager.writeDraft({
			kind,
			subnetId: identity.subnetId,
			nodeId: identity.nodeId,
			name,
			archive,
		})
		const keyPrefix = kind === 'skills' ? SKILL_FORGE_KEY_PREFIX : SCENARIO_FORGE_KEY_PREFIX
		await redisClient.set(
			`${keyPrefix}:${identity.subnetId}:${identity.nodeId}:${name}`,
			JSON.stringify({ stored_path: result.storedPath, commit: result.commitSha, ts: Date.now() }),
		)
		console.log(
			'draft stored',
			JSON.stringify({
				kind,
				subnetId: identity.subnetId,
				nodeId: identity.nodeId,
				name,
				bytes: archive.length,
				ms: Date.now() - started,
				commit: result.commitSha,
			}),
		)
		res.json({ ok: true, stored_path: result.storedPath, commit: result.commitSha })
        } catch (error) {
                console.error('failed to store draft', error)
                handleError(req, res, error, { status: 500, code: 'draft_store_failed' })
        }
}

mtlsRouter.post('/skills/draft', createDraftHandler('skills'))
mtlsRouter.post('/scenarios/draft', createDraftHandler('scenarios'))

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
        res.json({ ok: true, pr_url: 'https://github.com/stipot-com/adaos-registry/pull/mock-skill' })
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
	res.json({ ok: true, pr_url: 'https://github.com/stipot-com/adaos-registry/pull/mock-scenario' })
})

app.use('/v1', mtlsRouter)

function isValidGuid(guid: string) {
	return /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(guid)
}

const openedStreams: OpenedStreams = {}
const FILESPATH = '/tmp/inimatic_public_files/'

if (!fs.existsSync(FILESPATH)) {
	fs.mkdirSync(FILESPATH)
}

function cleanupSessionBucket(sessionId: string) {
	if (openedStreams[sessionId] && !Object.keys(openedStreams[sessionId]).length) {
		delete openedStreams[sessionId]
	}
}

const safeBasename = (value: string) => path.basename(value)

function saveFileChunk(sessionId: string, fileName: string, content: Array<number>) {
	if (!openedStreams[sessionId]) {
		openedStreams[sessionId] = {}
	}

	const safeFile = safeBasename(fileName)

	if (!openedStreams[sessionId][safeFile]) {
		const timestamp = String(Date.now())
		const stream = fs.createWriteStream(FILESPATH + timestamp + '_' + safeFile)
		const destroyTimeout = setTimeout(() => {
			openedStreams[sessionId][safeFile].stream.destroy()
			fs.unlink(FILESPATH + openedStreams[sessionId][safeFile].timestamp + '_' + safeFile, () => { })
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
			FILESPATH + openedStreams[sessionId][safeFile].timestamp + '_' + safeFile,
			(error) => {
				if (error) console.log(error)
			},
		)
		delete openedStreams[sessionId][safeFile]
		cleanupSessionBucket(sessionId)
		console.log('destroy', openedStreams)
	}, 30000)

	return new Promise<void>((resolve, reject) =>
		openedStreams[sessionId][safeFile].stream.write(
			new Uint8Array(content),
			(error) => (error ? reject(error) : resolve()),
		),
	)
}

const registerSocketHandlers = (socket: Socket) => {
        const namespace = socket.nsp
        console.log(socket.id)

        if (namespace.name === SOCKET_CHANNEL_NS) {
                socket.emit('channel_version', SOCKET_CHANNEL_VERSION)
        }

        socket.on('disconnecting', async () => {
                const rooms = Array.from(socket.rooms).filter((roomId) => roomId != socket.id)
                if (!rooms.length) return

                const sessionId = rooms[0]
                console.log('disconnect', socket.id, socket.rooms, sessionId)
                const sessionData: UnionSessionData = JSON.parse((await redisClient.get(sessionId))!)

                if (sessionData == null) {
                        socket.to(sessionId).emit('initiator_disconnect')
                        return
                }

                const isInitiator = sessionData.initiatorSocketId === socket.id
                if (isInitiator) {
                        if (sessionData.type === 'public') {
                                await Promise.all(
                                        sessionData.fileNames.map((item) => {
                                                const filePath = FILESPATH + item.timestamp + '_' + item.fileName

                                                return new Promise<void>((resolve) => fs.unlink(filePath, () => resolve()))
                                        }),
                                )
                        }

                        socket.to(sessionId).emit('initiator_disconnect')
                        namespace.socketsLeave(sessionId)
                        await redisClient.del(sessionId)
                } else {
                        namespace.to(sessionData.initiatorSocketId).emit(
                                'follower_disconnect',
                                sessionData.followers[socket.id],
                        )

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

        async function distributeSessionFiles(targetSocket: Socket, fileNames: Array<{ fileName: string; timestamp: string }>) {
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

        socket.on('set_session_data', async (sessionId: string, sessionData: SessionData) => {
                if (!isValidGuid(sessionId)) {
                        console.error('sessionId must be in guid format')
                        return
                }

                await redisClient.set(sessionId, JSON.stringify(sessionData))
                await redisClient.expire(sessionId, 3600)
        })

        socket.on('join_session', async ({ followerName, sessionId }: FollowerData) => {
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
                namespace.to(sessionData.initiatorSocketId).emit('follower_connect', followerName)
        })

        socket.on('set_session_public_files', async ({ sessionId, fileNames }) => {
                const sessionString = await redisClient.get(sessionId)
                const sessionData: PublicSessionData = sessionString ? JSON.parse(sessionString) : null

                if (sessionData == null) {
                        socket.emit('session_unavailable')
                        return
                }

                sessionData.fileNames = fileNames
                await redisClient.set(sessionId, JSON.stringify(sessionData))
        })

        socket.on('communication', async ({ isInitiator, sessionId, data }: CommunicationData) => {
                if (!isValidGuid(sessionId)) {
                        console.error('sessionId must be in guid format')
                        return
                }

                if (isInitiator) {
                        const firstValue = data['values'][0]

                        if (firstValue['type'] === 'transferFile') {
                                const safeFileName = safeBasename(firstValue['fileName'])
                                const pathToFile = FILESPATH + firstValue['timestamp'] + '_' + safeFileName
                                await new Promise<void>((resolve) =>
                                        fs.unlink(pathToFile, () => resolve()),
                                )
                                delete openedStreams[sessionId][safeFileName]
                                cleanupSessionBucket(sessionId)
                        }

                        namespace.to(sessionId).emit('communication', data)
                        return
                }

                const sessionData = (await redisClient.get(sessionId))!
                const initiatorSocketId = (JSON.parse(sessionData) as SessionData).initiatorSocketId

                const messageType = data['type']

                if (messageType === 'writeFile') {
                        await saveFileChunk(sessionId, data['fileName'], data['content'])
                }

                namespace.to(initiatorSocketId).emit('communication', data)
        })
}

io.on('connection', registerSocketHandlers)

if (SOCKET_CHANNEL_NS !== '/') {
        const nsv1 = io.of(SOCKET_CHANNEL_NS)
        nsv1.use((socket, next) => next())
        nsv1.on('connection', (socket) => registerSocketHandlers(socket))
}

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
	console.log(`AdaOS backhand listening on https://${HOST}:${PORT}`)
})
