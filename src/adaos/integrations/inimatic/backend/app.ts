import express from 'express'
import http from 'http'
import path from 'path'
import { v4 as uuidv4 } from 'uuid'
import { Server, Socket } from 'socket.io'
import { createClient } from 'redis'
import fs from 'fs'
import { mkdir, stat, writeFile } from 'fs/promises'
import { installAdaosBridge } from './adaos-bridge.js'

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


const app = express()
app.use(express.json({ limit: '32mb' }))

const server = http.createServer(app)
const io = new Server(server, {
        cors: { origin: '*' },
        pingTimeout: 10000,
        pingInterval: 10000,
})

installAdaosBridge(app, server)
const url = `redis://${process.env['PRODUCTION'] ? 'redis' : 'localhost'}:6379`
const redisClient = await createClient({ url })
        .on('error', (err) => console.log('Redis Client Error', err))
        .connect()

const ROOT_TOKEN = process.env['ROOT_TOKEN'] ?? 'dev-root-token'
const FORGE_ROOT = '/tmp/forge'
const SKILL_FORGE_KEY_PREFIX = 'forge:skills'
const TTL_SUBNET_SECONDS = 60 * 60 * 24 * 7
const TTL_NODE_SECONDS = 60 * 60 * 24

await mkdir(FORGE_ROOT, { recursive: true })

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
        if (openedStreams[sessionId] && !Object.keys(openedStreams[sessionId]).length) {
                delete openedStreams[sessionId]
        }
}

function assertSafeName(name: string) {
        if (!name || name.includes('..') || name.includes('/') || name.includes('\\')) {
                throw new Error('invalid name')
        }
        if (path.basename(name) !== name) {
                throw new Error('invalid name')
        }
}

const safeBasename = (value: string) => path.basename(value)

const ensureForgeDir = async (nodeId: string, kind: 'skills' | 'scenarios', name: string) => {
        const dir = path.join(FORGE_ROOT, nodeId, kind, name)
        await mkdir(dir, { recursive: true })
        return dir
}

const generateSubnetId = () => `sn_${uuidv4().replace(/-/g, '').slice(0, 8)}`
const generateNodeId = () => `node_${uuidv4().replace(/-/g, '').slice(0, 8)}`

function saveFileChunk(sessionId: string, fileName: string, content: Array<number>) {
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
                                () => {}
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

io.on('connect', (socket) => {
	console.log(socket.id)

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

		let isInitiator = sessionData.initiatorSocketId === socket.id
		if (isInitiator) {
			if (sessionData.type === 'public') {
				await Promise.all(
					sessionData.fileNames.map((item) => {
						const path =
							FILESPATH + item.timestamp + '_' + item.fileName

						return new Promise<void>((resolve) =>
							fs.unlink(path, () => resolve())
						)
					})
				)
			}

			socket.to(sessionId).emit('initiator_disconnect')
			io.socketsLeave(sessionId)
			await redisClient.del(sessionId)
			// delete saved files
		} else {
			io.to(sessionData.initiatorSocketId).emit(
				'follower_disconnect',
				sessionData.followers[socket.id]
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

	async function distributeSessionFiles(
		socket: Socket,
		fileNames: Array<{ fileName: string; timestamp: string }>
	) {
		const chunksize = 64 * 1024

		for (let item of fileNames) {
			const path = FILESPATH + item.timestamp + '_' + item.fileName
			const readStream = fs.createReadStream(path, {
				highWaterMark: chunksize,
				// encoding: 'utf8',
			})

			const size = (await stat(path)).size

			await sendToPublicFollower(socket, {
				type: 'transferFile',
				fileName: item.fileName,
				size: size,
			})
			console.log(size)

			for await (const chunk of readStream) {
				console.log('chunk', typeof chunk)

				await sendToPublicFollower(socket, {
					type: 'transferFile',
					fileName: item.fileName,
					size: size,
					content: new Uint8Array(chunk),
				})
			}

                        await sendToPublicFollower(socket, {
                                type: 'transferFile',
                                fileName: item.fileName,
                                size: size,
                                end: true,
                        })
                }
        }

	socket.on('add_follower', async (data) => {
		// возможно, стоит проверять наличие других комнат у сокета,
		// чтоб не было лишних подключений
		const { followerName, sessionId }: FollowerData = data
		if (!isValidGuid(sessionId)) return

		const sessionData: UnionSessionData = JSON.parse(
			(await redisClient.get(sessionId))!
		)
		console.log('add follower', followerName)

		if (sessionData === null) {
			socket.emit('initiator_disconnect')
			return
		}

		sessionData.followers[socket.id] = followerName
		socket.join(sessionId)

		await redisClient.set(sessionId, JSON.stringify(sessionData))

		io.to(sessionData.initiatorSocketId).emit(
			'connect_follower',
			followerName
		)

		if (sessionData.type === 'public') {
			await distributeSessionFiles(socket, sessionData.fileNames)
		}
	})

	socket.on('disconnect_follower', async (data) => {
		const { followerName, sessionId, isInitiator } = data
		if (!isValidGuid(sessionId)) return

		const sessionData: SessionData = JSON.parse(
			(await redisClient.get(sessionId))!
		)

		if (sessionData == null) {
			socket.emit('initiator_disconnect')
			return
		}

		if (isInitiator) {
			const socketIds = Object.keys(sessionData.followers).filter(
				(followerSocketId) =>
					sessionData.followers[followerSocketId] === followerName
			)
			console.log(socketIds)

			if (socketIds.length === 1) {
				delete sessionData.followers[socketIds[0]]
				await redisClient.set(sessionId, JSON.stringify(sessionData))
				const sockets = await io.sockets.fetchSockets()
				const followerSocket = sockets.filter(
					(socket) => socket.id === socketIds[0]
				)[0]
				if (followerSocket) {
					followerSocket.leave(sessionId)
					followerSocket.emit('initiator_disconnect')
				}
				socket.emit('follower_disconnect', followerName)
			}
		} else {
			delete sessionData.followers[socket.id]
			await redisClient.set(sessionId, JSON.stringify(sessionData))
			socket.leave(sessionId)
			socket.emit('initiator_disconnect')
			io.to(sessionData.initiatorSocketId).emit(
				'follower_disconnect',
				followerName
			)
		}
	})

	socket.on('conductor', async (data, fn) => {
		const receivedData: CommunicationData = data

		const sessionData: UnionSessionData = JSON.parse(
			(await redisClient.get(receivedData.sessionId))!
		)

		if (sessionData == null) {
			socket.to(receivedData.sessionId).emit('initiator_disconnect')
			return
		}

		if (sessionData.type === 'public') {
			const dataBody = receivedData.data
			if (dataBody.type === 'transferFile') {
                                await saveFileChunk(
                                        receivedData.sessionId,
                                        dataBody.fileName,
                                        dataBody.content
                                )

                                if (dataBody.end) {
                                        clearTimeout(
                                                openedStreams[receivedData.sessionId][path.basename(dataBody.fileName)]
                                                        .destroyTimeout
                                        )
                                        await new Promise<void>((resolve) =>
                                                openedStreams[receivedData.sessionId][
                                                        path.basename(dataBody.fileName)
                                                ].stream.close(() => resolve())
                                        )
                                        sessionData.fileNames.push({
                                                fileName: dataBody.fileName,
                                                timestamp:
                                                        openedStreams[receivedData.sessionId][
                                                                path.basename(dataBody.fileName)
                                                        ].timestamp,
                                        })
                                        await redisClient.set(
                                                receivedData.sessionId,
                                                JSON.stringify(sessionData)
                                        )
                                        delete openedStreams[receivedData.sessionId][
                                                path.basename(dataBody.fileName)
                                        ]
                                        cleanupSessionBucket(receivedData.sessionId)
                                        io.to(sessionData.initiatorSocketId).emit(
                                                'saved_file',
                                                dataBody.fileName
					)
				}
			}
		}
		await new Promise<void>((resolve) => {
			if (receivedData.isInitiator) {
                                socket
                                        .to(receivedData.sessionId)
                                        .emit('communication', receivedData.data, () => resolve())
                        } else {
                                io.to(sessionData.initiatorSocketId).emit(
                                        'communication',
                                        receivedData.data,
                                        () => resolve()
                                )
			}
		})

		if (fn) {
			fn(1)
		}
	})
})

const PORT = parseInt(process.env['PORT'] || '3030')
const HOST = process.env['HOST'] || '0.0.0.0'

const tokenGuard: express.RequestHandler = (req, res, next) => {
        const token = req.header('X-AdaOS-Token')
        if (!token || token !== ROOT_TOKEN) {
                res.status(401).json({ error: 'unauthorized' })
                return
        }
        next()
}

const v1Router = express.Router()

v1Router.post('/subnets/register', async (req, res) => {
        try {
                const subnetId = generateSubnetId()
                const subnetName: string | undefined = req.body?.subnet_name
                const payload = {
                        subnet_id: subnetId,
                        subnet_name: subnetName ?? null,
                        created_at: new Date().toISOString(),
                }
                await redisClient.set(`subnet:${subnetId}`, JSON.stringify(payload), {
                        EX: TTL_SUBNET_SECONDS,
                })
                res.json({ subnet_id: subnetId })
        } catch (error: any) {
                res.status(500).json({ error: 'subnet_register_failed', detail: String(error?.message ?? error) })
        }
})

v1Router.post('/nodes/register', async (req, res) => {
        try {
                let subnetId: string | undefined = req.body?.subnet_id
                if (!subnetId) {
                        subnetId = generateSubnetId()
                        await redisClient.set(
                                `subnet:${subnetId}`,
                                JSON.stringify({
                                        subnet_id: subnetId,
                                        subnet_name: null,
                                        created_at: new Date().toISOString(),
                                }),
                                { EX: TTL_SUBNET_SECONDS }
                        )
                }
                const nodeId = generateNodeId()
                await redisClient.set(
                        `node:${nodeId}`,
                        JSON.stringify({
                                node_id: nodeId,
                                subnet_id: subnetId,
                                created_at: new Date().toISOString(),
                        }),
                        { EX: TTL_NODE_SECONDS }
                )
                res.json({ node_id: nodeId, subnet_id: subnetId })
        } catch (error: any) {
                        res.status(500).json({ error: 'node_register_failed', detail: String(error?.message ?? error) })
        }
})

const SKILL_TEMPLATES = [
        {
                id: 'python-minimal',
                name: 'Python Minimal Skill',
                description: 'Starter AdaOS skill using Python and minimal dependencies.',
        },
        {
                id: 'node-basic',
                name: 'Node.js Basic Skill',
                description: 'Sample JavaScript skill scaffold with TypeScript typings.',
        },
]

const SCENARIO_TEMPLATES = [
        {
                id: 'blank-scenario',
                name: 'Blank Scenario',
                description: 'Empty scenario template ready for customization.',
        },
        {
                id: 'morning-routine',
                name: 'Morning Routine',
                description: 'Greets the user and prepares devices for the day.',
        },
]

v1Router.get('/templates', (req, res) => {
        const type = String(req.query?.type ?? '').toLowerCase()
        if (type === 'skill') {
                res.json({ items: SKILL_TEMPLATES })
                return
        }
        if (type === 'scenario') {
                res.json({ items: SCENARIO_TEMPLATES })
                return
        }
        res.status(400).json({ error: 'unknown_template_type' })
})

v1Router.post('/skills/draft', async (req, res) => {
        try {
                const body = req.body ?? {}
                const nodeId = body.node_id as string | undefined
                const name = body.name as string | undefined
                const archiveB64 = body.archive_b64 as string | undefined

                if (!nodeId || !name || !archiveB64) {
                        res.status(400).json({ error: 'missing_parameters' })
                        return
                }

                assertSafeName(name)

                const forgeDir = await ensureForgeDir(nodeId, 'skills', name)
                const timestamp = Date.now()
                const storedPath = path.join(forgeDir, `${timestamp}_${name}.zip`)
                const buffer = Buffer.from(archiveB64, 'base64')
                await writeFile(storedPath, buffer)

                const redisKey = `${SKILL_FORGE_KEY_PREFIX}:${nodeId}:${name}`
                await redisClient.lPush(redisKey, storedPath)

                res.json({ ok: true, stored: storedPath })
        } catch (error: any) {
                const detail = String(error?.message ?? error)
                const status = detail === 'invalid name' ? 400 : 500
                res.status(status).json({ error: 'skill_draft_failed', detail })
        }
})

v1Router.post('/skills/pr', async (req, res) => {
        try {
                        const body = req.body ?? {}
                        const nodeId = body.node_id as string | undefined
                        const name = body.name as string | undefined
                        if (!nodeId || !name) {
                                res.status(400).json({ error: 'missing_parameters' })
                                return
                        }
                        const targetBranch = (body.target_branch as string | undefined) ?? 'main'
                        const prUrl = `https://example.com/mock/pr/${encodeURIComponent(name)}/${Date.now()}`
                        res.json({ ok: true, pr_url: prUrl, target_branch: targetBranch })
        } catch (error: any) {
                        res.status(500).json({ error: 'skill_pr_failed', detail: String(error?.message ?? error) })
        }
})

app.use('/v1', tokenGuard, v1Router)

app.use((req, res) => {
        res.status(404).send('Resource not found')
})

server.listen(PORT, HOST, () =>
        console.log(`Started on http://${HOST}:${PORT} ...`)
)

const closeStreams = () => {
        for (const sessionId of Object.keys(openedStreams)) {
                for (const fileName of Object.keys(openedStreams[sessionId])) {
                        const info = openedStreams[sessionId][fileName]
                        clearTimeout(info.destroyTimeout)
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
