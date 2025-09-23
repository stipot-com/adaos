import 'dotenv/config'
import express from 'express'
import https from 'https'
import path from 'path'
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

declare global {
	namespace Express {
		interface Request {
			auth?: ClientIdentity
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
app.use(express.json({ limit: '64mb' }))

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
})

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

function generateSubnetId(): string {
	return `sn_${uuidv4().replace(/-/g, '').slice(0, 8)}`
}

function generateNodeId(): string {
	return `node_${uuidv4().replace(/-/g, '').slice(0, 8)}`
}

function assertSafeName(name: string): void {
	if (!name || name.includes('..') || name.includes('/') || name.includes('\\')) {
		throw new Error('invalid name')
	}
	if (path.basename(name) !== name) throw new Error('invalid name')
}


function decodeArchive(archiveB64: string): Buffer {
	try {
		return Buffer.from(archiveB64, 'base64')
	} catch (error) {
		throw new Error('invalid archive encoding')
	}
}

function verifySha256(buffer: Buffer, expected?: string): void {
	if (!expected) {
		return
	}
	const normalized = expected.trim().toLowerCase()
	const actual = createHash('sha256').update(buffer).digest('hex').toLowerCase()
	if (normalized !== actual) {
		throw new Error('sha256 mismatch')
	}
}

async function useBootstrapToken(token: string): Promise<Record<string, unknown>> {
	const key = `bootstrap:${token}`
	const value = await redisClient.get(key)
	if (!value) {
		throw new Error('invalid bootstrap token')
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

app.get('/healthz', (_req, res) => {
	res.json({ ok: true, time: new Date().toISOString(), mtls: true })
})

app.post('/v1/bootstrap_token', async (req, res) => {
	const token = req.header('X-Root-Token') ?? ''
	if (!token || token !== ROOT_TOKEN) {
		res.status(401).json({ error: 'unauthorized' })
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
		res.status(401).json({ error: 'bootstrap token required' })
		return
	}
	try {
		await useBootstrapToken(bootstrapToken)
	} catch (error) {
		res.status(401).json({ error: 'invalid bootstrap token' })
		return
	}

	const csrPem = typeof req.body?.csr_pem === 'string' ? req.body.csr_pem : null
	if (!csrPem) {
		res.status(400).json({ error: 'csr_pem is required' })
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
		res.status(400).json({ error: error instanceof Error ? error.message : 'certificate issue failed' })
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
			res.status(401).json({ error: 'invalid bootstrap token' })
			return
		}
		const bodySubnet = typeof req.body?.subnet_id === 'string' ? req.body.subnet_id : undefined
		if (!bodySubnet) {
			res.status(400).json({ error: 'subnet_id is required when using bootstrap token' })
			return
		}
		subnetId = bodySubnet
	} else {
		const identity = getClientIdentity(req)
		if (!identity || identity.type !== 'hub') {
			res.status(401).json({ error: 'hub client certificate required' })
			return
		}
		subnetId = identity.subnetId
		const bodySubnet = typeof req.body?.subnet_id === 'string' ? req.body.subnet_id : undefined
		if (bodySubnet && bodySubnet !== subnetId) {
			res.status(400).json({ error: 'subnet_id does not match certificate' })
			return
		}
	}

	if (!subnetId) {
		res.status(400).json({ error: 'subnet_id is required' })
		return
	}

	const csrPem = typeof req.body?.csr_pem === 'string' ? req.body.csr_pem : null
	if (!csrPem) {
		res.status(400).json({ error: 'csr_pem is required' })
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
		res.status(400).json({ error: error instanceof Error ? error.message : 'certificate issue failed' })
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
		res.status(401).json({ error: 'client certificate required' })
		return
	}
	const identity = getClientIdentity(req)
	if (!identity) {
		res.status(403).json({ error: 'invalid client certificate' })
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
		res.status(403).json({ error: 'node client certificate required' })
		return
	}

	const nodeId = typeof req.body?.node_id === 'string' ? req.body.node_id : ''
	if (!nodeId || nodeId !== identity.nodeId) {
		res.status(403).json({ error: 'node_id mismatch' })
		return
	}
	const name = typeof req.body?.name === 'string' ? req.body.name : ''
	const archiveB64 = typeof req.body?.archive_b64 === 'string' ? req.body.archive_b64 : ''
	const sha256 = typeof req.body?.sha256 === 'string' ? req.body.sha256 : undefined
	if (!name || !archiveB64) {
		res.status(400).json({ error: 'name and archive_b64 are required' })
		return
	}

	let archive: Buffer
	try {
		assertSafeName(name)
		archive = decodeArchive(archiveB64)
		if (!archive.length) {
			throw new Error('archive is empty')
		}
		if (archive.length > MAX_ARCHIVE_BYTES) {
			res.status(413).json({ error: 'archive exceeds allowed size' })
			return
		}
		verifySha256(archive, sha256)
	} catch (error) {
		res.status(400).json({ error: error instanceof Error ? error.message : 'invalid archive' })
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
		res.status(500).json({ error: 'failed to store draft' })
	}
}

mtlsRouter.post('/skills/draft', createDraftHandler('skills'))
mtlsRouter.post('/scenarios/draft', createDraftHandler('scenarios'))

mtlsRouter.post('/skills/pr', (req, res) => {
	const identity = req.auth
	if (!identity || identity.type !== 'hub') {
		res.status(403).json({ error: 'hub client certificate required' })
		return
	}
	const name = typeof req.body?.name === 'string' ? req.body.name : ''
	const nodeId = typeof req.body?.node_id === 'string' ? req.body.node_id : ''
	if (!name || !nodeId) {
		res.status(400).json({ error: 'name and node_id are required' })
		return
	}
	res.json({ ok: true, pr_url: 'https://github.com/stipot-com/adaos-registry/pull/mock-skill' })
})

mtlsRouter.post('/scenarios/pr', (req, res) => {
	const identity = req.auth
	if (!identity || identity.type !== 'hub') {
		res.status(403).json({ error: 'hub client certificate required' })
		return
	}
	const name = typeof req.body?.name === 'string' ? req.body.name : ''
	const nodeId = typeof req.body?.node_id === 'string' ? req.body.node_id : ''
	if (!name || !nodeId) {
		res.status(400).json({ error: 'name and node_id are required' })
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

io.on('connection', (socket) => {
	console.log(socket.id)

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
			io.socketsLeave(sessionId)
			await redisClient.del(sessionId)
		} else {
			io.to(sessionData.initiatorSocketId).emit(
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

	async function sendToPublicFollower(socket: Socket, emitObject: any) {
		return new Promise<void>((resolve) => {
			socket.emit('communication', emitObject, () => resolve())
		})
	}

	async function distributeSessionFiles(socket: Socket, fileNames: Array<{ fileName: string; timestamp: string }>) {
		const chunksize = 64 * 1024

		for (const item of fileNames) {
			const filePath = FILESPATH + item.timestamp + '_' + item.fileName
			const readStream = fs.createReadStream(filePath, {
				highWaterMark: chunksize,
			})

			const size = (await stat(filePath)).size

			await sendToPublicFollower(socket, {
				type: 'transferFile',
				fileName: item.fileName,
				size,
			})

			for await (const chunk of readStream) {
				await sendToPublicFollower(socket, {
					type: 'writeFile',
					fileName: item.fileName,
					content: Array.from(new Uint8Array(chunk as Buffer)),
					size,
				})
			}

			await sendToPublicFollower(socket, {
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
		io.to(sessionData.initiatorSocketId).emit('follower_connect', followerName)
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

			io.to(sessionId).emit('communication', data)
			return
		}

		const sessionData = (await redisClient.get(sessionId))!
		const initiatorSocketId = (JSON.parse(sessionData) as SessionData).initiatorSocketId

		const messageType = data['type']

		if (messageType === 'writeFile') {
			await saveFileChunk(sessionId, data['fileName'], data['content'])
		}

		io.to(initiatorSocketId).emit('communication', data)
	})
})

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
