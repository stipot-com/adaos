import fs from 'node:fs'
import { mkdir, rm, writeFile } from 'node:fs/promises'
import path from 'node:path'

import AdmZip from 'adm-zip'
import { simpleGit, type SimpleGit } from 'simple-git'

export type DraftKind = 'skills' | 'scenarios'

export type ForgeManagerOptions = {
	repoUrl: string
	workdir?: string
	authorName: string
	authorEmail: string
	sshKeyPath?: string
}

export type WriteDraftOptions = {
	kind: DraftKind
	subnetId: string
	nodeId: string
	name: string
	archive: Buffer
	saveUpload?: boolean
}

export type DraftWriteResult = {
	storedPath: string
	commitSha: string
}

const DEFAULT_WORKDIR = '/var/lib/adaos/forge'

class Mutex {
	private current: Promise<void> = Promise.resolve()

	async runExclusive<T>(callback: () => Promise<T>): Promise<T> {
		const release = (() => {
			let resolver: (() => void) | undefined
			const promise = new Promise<void>((resolve) => {
				resolver = resolve
			})
			return { promise, resolver: resolver! }
		})()

		const previous = this.current
		this.current = previous.then(() => release.promise)
		await previous
		try {
			return await callback()
		} finally {
			release.resolver()
		}
	}
}

export class ForgeManager {
	private readonly options: Required<Omit<ForgeManagerOptions, 'sshKeyPath'>> & { sshKeyPath?: string }
	private git: SimpleGit | null = null
	private readonly mutex = new Mutex()
	private initialized = false

	constructor(options: ForgeManagerOptions) {
		const workdir = options.workdir ?? DEFAULT_WORKDIR
		this.options = {
			repoUrl: options.repoUrl,
			workdir,
			authorName: options.authorName,
			authorEmail: options.authorEmail,
			sshKeyPath: options.sshKeyPath,
		}
	}

	async ensureReady(): Promise<void> {
		await this.mutex.runExclusive(async () => {
			if (this.initialized) {
				return
			}
			const env = this.gitEnv()
			const gitDir = path.join(this.options.workdir, '.git')
                        if (!fs.existsSync(gitDir)) {
                                const parent = path.dirname(this.options.workdir)
                                await mkdir(parent, { recursive: true })
                                const workdirExists = fs.existsSync(this.options.workdir)
                                if (workdirExists) {
                                        const entries = fs.readdirSync(this.options.workdir)
                                        if (entries.length > 0) {
                                                throw new Error(
                                                        `forge workdir ${this.options.workdir} exists and is not empty`
                                                )
                                        }
                                }
                                const parentGit = simpleGit({ baseDir: parent })
                                parentGit.env(env)
                                await parentGit.clone(this.options.repoUrl, this.options.workdir)
                        }

                        await mkdir(this.options.workdir, { recursive: true })
                        this.git = simpleGit({ baseDir: this.options.workdir })
                        this.git.env(env)
			await this.git.addConfig('user.name', this.options.authorName)
			await this.git.addConfig('user.email', this.options.authorEmail)
			await this.sync()
			this.initialized = true
		})
	}

	async ensureSubnet(subnetId: string): Promise<string> {
		return this.mutex.runExclusive(async () => {
			await this.ensureReady()
			const dir = path.join(this.options.workdir, 'subnets', subnetId)
			const keepPath = path.join(dir, '.keep')
			const existed = fs.existsSync(dir)
			await mkdir(dir, { recursive: true })
			if (!fs.existsSync(keepPath)) {
				await writeFile(keepPath, `subnet ${subnetId}\n`)
			}
			if (!existed) {
				await this.stageAll()
				const commit = await this.commit(`init subnet ${subnetId}`)
				await this.push()
				return commit
			}
			return ''
		})
	}

	async ensureNode(subnetId: string, nodeId: string): Promise<string> {
		return this.mutex.runExclusive(async () => {
			await this.ensureReady()
			const dir = path.join(this.options.workdir, 'subnets', subnetId, 'nodes', nodeId)
			const keepPath = path.join(dir, '.keep')
			const existed = fs.existsSync(dir)
			await mkdir(dir, { recursive: true })
			if (!fs.existsSync(keepPath)) {
				await writeFile(keepPath, `node ${nodeId}\n`)
			}
			if (!existed) {
				await this.stageAll()
				const commit = await this.commit(`register node ${nodeId}`)
				await this.push()
				return commit
			}
			return ''
		})
	}

	async writeDraft(options: WriteDraftOptions): Promise<DraftWriteResult> {
		const { kind, subnetId, nodeId, name, archive } = options
		return this.mutex.runExclusive(async () => {
			await this.ensureReady()
			const relativeBase = path.join('subnets', subnetId, 'nodes', nodeId)
			const targetDir = path.join(this.options.workdir, relativeBase, kind, name)
			await rm(targetDir, { recursive: true, force: true })
			await mkdir(targetDir, { recursive: true })

			const zip = new AdmZip(archive)
			for (const entry of zip.getEntries()) {
				const entryName = entry.entryName
				if (!entryName || entryName.includes('..')) {
					throw new Error('archive entry has forbidden name')
				}
				const destination = path.join(targetDir, entryName)
				const normalized = path.normalize(destination)
				if (!normalized.startsWith(targetDir)) {
					throw new Error('archive entry escapes target directory')
				}
				if (entry.isDirectory) {
					await mkdir(normalized, { recursive: true })
				} else {
					await mkdir(path.dirname(normalized), { recursive: true })
					const data = entry.getData()
					await writeFile(normalized, data)
				}
			}

			if (options.saveUpload ?? true) {
				const uploadsDir = path.join(this.options.workdir, relativeBase, 'uploads')
				await mkdir(uploadsDir, { recursive: true })
				const timestamp = Date.now()
				const safeName = name.replace(/[^a-zA-Z0-9._-]/g, '_')
				const uploadPath = path.join(uploadsDir, `${timestamp}_${safeName}.zip`)
				await writeFile(uploadPath, archive)
			}

			await this.stageAll()
			const commitSha = await this.commit(`node:${nodeId} ${kind.slice(0, -1)}:${name} draft`)
			await this.push()
			return {
				storedPath: path.join(relativeBase, kind, name),
				commitSha,
			}
		})
	}

	private gitEnv(): NodeJS.ProcessEnv {
		if (!this.options.sshKeyPath) {
			return process.env
		}
		const command = `ssh -i ${this.options.sshKeyPath} -o IdentitiesOnly=yes -o StrictHostKeyChecking=no`
		return { ...process.env, GIT_SSH_COMMAND: command }
	}

	private async sync(): Promise<void> {
		if (!this.git) return
		await this.git.fetch(['--all'])
		await this.git.reset(['--hard', 'origin/main'])
	}

	private async stageAll(): Promise<void> {
		if (!this.git) {
			throw new Error('forge git is not initialized')
		}
		await this.git.add('.')
	}

	private async commit(message: string): Promise<string> {
		if (!this.git) {
			throw new Error('forge git is not initialized')
		}
		const status = await this.git.status()
		if (status.isClean()) {
			const log = await this.git.log({ n: 1 })
			return log.latest?.hash ?? ''
		}
		const result = await this.git.commit(message)
		return result.commit
	}

	private async push(): Promise<void> {
		if (!this.git) {
			throw new Error('forge git is not initialized')
		}
		await this.git.push('origin', 'HEAD:main')
	}
}
