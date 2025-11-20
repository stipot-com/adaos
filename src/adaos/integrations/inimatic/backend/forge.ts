// src/adaos/integrations/inimatic/backend/forge.ts
import fs from 'node:fs';
import { mkdir, readdir, rm, writeFile } from 'node:fs/promises';
import path, { resolve as pathResolve } from 'node:path';
import { randomUUID } from 'node:crypto';

import AdmZip from 'adm-zip';
import { simpleGit, type SimpleGit } from 'simple-git';

import { readFileSync } from 'node:fs';
import { getInstallationToken } from './github-app.js';

export type DraftKind = 'skills' | 'scenarios';

type GhAppEnv = {
	appId?: string;              // GH_APP_ID
	instId?: string;             // GH_APP_INSTALLATION_ID
	privateKeyPath?: string;     // GH_APP_PRIVATE_KEY_FILE (путь к PEM)
	repo?: string;               // FORGE_REPO, формат "owner/repo"
};

export type ForgeManagerOptions = {
	repoUrl: string;
	workdir?: string;
	authorName: string;
	authorEmail: string;
	sshKeyPath?: string;
};

export type WriteDraftOptions = {
	kind: DraftKind;
	subnetId: string;
	nodeId: string;
	name: string;
	archive: Buffer;
	saveUpload?: boolean;
};

export type DraftWriteResult = {
        storedPath: string;
        commitSha: string;
};

export type DeleteDraftArgs = {
        kind: DraftKind;
        subnetId: string;
        name: string;
        nodeId?: string;
        allNodes?: boolean;
};

export type DeleteDraftResult = {
        deleted: Array<{ node_id: string; storedPath?: string }>;
        redisKeys?: string[];
        auditId: string;
};

export type DeleteRegistryArgs = {
        kind: DraftKind;
        subnetId: string;
        name: string;
        version?: string;
        allVersions?: boolean;
        force?: boolean;
};

export type DeleteRegistryResult = {
        deleted: Array<{ version: string }>;
        skipped?: Array<{ version: string; reason: string }>;
        tombstoned?: boolean;
        auditId: string;
};

const DEFAULT_WORKDIR = '/var/lib/adaos/forge';

class Mutex {
	private current: Promise<void> = Promise.resolve();

	async runExclusive<T>(callback: () => Promise<T>): Promise<T> {
		const release = (() => {
			let resolver: (() => void) | undefined;
			const promise = new Promise<void>((resolve) => {
				resolver = resolve;
			});
			return { promise, resolver: resolver! };
		})();

		const previous = this.current;
		this.current = previous.then(() => release.promise);
		await previous;
		try {
			return await callback();
		} finally {
			release.resolver();
		}
	}
}

/** >>> Helpers for GitHub App flow <<< */

function ghEnv(): GhAppEnv {
	return {
		appId: process.env['GH_APP_ID'],
		instId: process.env['GH_APP_INSTALLATION_ID'],
		privateKeyPath: process.env['GH_APP_PRIVATE_KEY_FILE'],
		repo: process.env['FORGE_REPO'], // "owner/repo"
	};
}


function parseRepoOwnerNameFromUrl(url: string): string | undefined {
	// Supports:
	//  - https://github.com/owner/repo.git
	//  - https://github.com/owner/repo
	//  - git@github.com:owner/repo.git
	//  - git@github.com:owner/repo
	try {
		if (url.startsWith('git@github.com:')) {
			const rest = url.replace('git@github.com:', '').replace(/\.git$/, '');
			const [owner, repo] = rest.split('/');
			if (owner && repo) return `${owner}/${repo}`;
			return undefined;
		}
		if (url.startsWith('https://github.com/')) {
			const rest = url.replace('https://github.com/', '').replace(/\.git$/, '');
			const [owner, repo] = rest.split('/');
			if (owner && repo) return `${owner}/${repo}`;
			return undefined;
		}
	} catch {
		// no-op
	}
	return undefined;
}

async function buildHttpsUrlWithToken(gh: GhAppEnv): Promise<string> {
	if (!gh.appId || !gh.instId || !gh.privateKeyPath) {
		throw new Error('GitHub App env is incomplete (need GH_APP_ID, GH_APP_INSTALLATION_ID, GH_APP_PRIVATE_KEY_FILE)');
	}
	const pem = readFileSync(pathResolve(gh.privateKeyPath), 'utf8');
	const token = await getInstallationToken({
		appId: gh.appId,
		installationId: gh.instId,
		privateKeyPem: pem,
	});
	const repo = gh.repo; // prefer FORGE_REPO if provided explicitly
	if (!repo) throw new Error('FORGE_REPO is required when using GitHub App (format "owner/repo")');
	return `https://x-access-token:${token}@github.com/${repo}.git`;
}

async function ensureOriginUrl(git: SimpleGit, url: string) {
	const remotes = await git.getRemotes(true);
	const hasOrigin = remotes.some((r) => r.name === 'origin');
	if (!hasOrigin) {
		await git.addRemote('origin', url);
	} else {
		await git.remote(['set-url', 'origin', url]);
	}
}

/** ------------------------------------------------------------ */

export class ForgeManager {
	private readonly options: Required<Omit<ForgeManagerOptions, 'sshKeyPath'>> & { sshKeyPath?: string };
	private git: SimpleGit | null = null;
	private readonly mutex = new Mutex();
	private initialized = false;
	private initPromise?: Promise<void>;

	constructor(options: ForgeManagerOptions) {
		const workdir = options.workdir ?? DEFAULT_WORKDIR;
		this.options = {
			repoUrl: options.repoUrl,
			workdir,
			authorName: options.authorName,
			authorEmail: options.authorEmail,
			sshKeyPath: options.sshKeyPath,
		};
	}

	private async doInit(): Promise<void> {
		const gh = ghEnv();
		const useGhApp = Boolean(gh.appId && gh.instId && gh.privateKeyPath);
		const env = this.gitEnv(useGhApp);

		const gitDir = path.join(this.options.workdir, '.git');

		if (!fs.existsSync(gitDir)) {
			const parent = path.dirname(this.options.workdir);
			await mkdir(parent, { recursive: true });
			const entries = fs.existsSync(this.options.workdir) ? fs.readdirSync(this.options.workdir) : [];
			if (entries.length > 0) {
				throw new Error(`forge workdir ${this.options.workdir} exists and is not empty`);
			}
			const parentGit = simpleGit({ baseDir: parent });
			parentGit.env(env);

			if (useGhApp) {
				if (!gh.repo) gh.repo = parseRepoOwnerNameFromUrl(this.options.repoUrl);
				const httpsUrl = await buildHttpsUrlWithToken(gh);
				await parentGit.clone(httpsUrl, this.options.workdir);
			} else {
				await parentGit.clone(this.options.repoUrl, this.options.workdir);
			}
		}

		await mkdir(this.options.workdir, { recursive: true });
		this.git = simpleGit({ baseDir: this.options.workdir });
		// TODO remove
		this.git.outputHandler((cmd, stdout, stderr) => {
			console.log('[forge/git]', cmd);
			stdout?.on('data', b => process.stdout.write('[forge/out] ' + b.toString()));
			stderr?.on('data', b => process.stderr.write('[forge/err] ' + b.toString()));
		});
		this.git.env(env);

		await this.git.addConfig('user.name', this.options.authorName);
		await this.git.addConfig('user.email', this.options.authorEmail);

		// низкоскоростной таймаут — чтобы не зависало
		await this.git.addConfig('http.lowSpeedLimit', '1').catch(() => { });
		await this.git.addConfig('http.lowSpeedTime', '30').catch(() => { });

		if (useGhApp) {
			if (!gh.repo) gh.repo = parseRepoOwnerNameFromUrl(this.options.repoUrl);
			const httpsUrl = await buildHttpsUrlWithToken(gh);
			await ensureOriginUrl(this.git, httpsUrl);
		}

		await this.sync(useGhApp);
		this.initialized = true;
	}

	async ensureReady(): Promise<void> {
		if (this.initialized) return;
		if (!this.initPromise) {
			this.initPromise = this.doInit().catch((e) => { // чтобы не залипало, если init упал
				this.initPromise = undefined;
				throw e;
			});
		}
		await this.initPromise;
	}

	async ensureSubnet(subnetId: string): Promise<string> {
		await this.ensureReady();
		return this.mutex.runExclusive(async () => {
			console.log(`[forge] ensureSubnet ${subnetId}`);
			const dir = path.join(this.options.workdir, 'subnets', subnetId);
			const keepPath = path.join(dir, '.keep');
			const existed = fs.existsSync(dir);
			await mkdir(dir, { recursive: true });
			if (!fs.existsSync(keepPath)) {
				await writeFile(keepPath, `subnet ${subnetId}\n`);
			}
			if (!existed) {
				await this.stageAll();
				const commit = await this.commit(`init subnet ${subnetId}`);
				await this.withFreshOrigin();
				await this.push();
				console.log(`[forge] ensureSubnet ${subnetId}`);
				return commit;
			}
			console.log(`[forge] ensureSubnet ${subnetId}`);
			return '';
		});
	}

	async ensureNode(subnetId: string, nodeId: string): Promise<string> {
		await this.ensureReady();
		return this.mutex.runExclusive(async () => {
			await this.ensureReady();
			const dir = path.join(this.options.workdir, 'subnets', subnetId, 'nodes', nodeId);
			const keepPath = path.join(dir, '.keep');
			const existed = fs.existsSync(dir);
			await mkdir(dir, { recursive: true });
			if (!fs.existsSync(keepPath)) {
				await writeFile(keepPath, `node ${nodeId}\n`);
			}
			if (!existed) {
				await this.stageAll();
				const commit = await this.commit(`register node ${nodeId}`);
				await this.push();
				return commit;
			}
			return '';
		});
	}

        async writeDraft(options: WriteDraftOptions): Promise<DraftWriteResult> {
                const { kind, subnetId, nodeId, name, archive } = options;
                await this.ensureReady();
                return this.mutex.runExclusive(async () => {
                        await this.ensureReady();
			const relativeBase = path.join('subnets', subnetId, 'nodes', nodeId);
			const targetDir = path.join(this.options.workdir, relativeBase, kind, name);
			await rm(targetDir, { recursive: true, force: true });
			await mkdir(targetDir, { recursive: true });

			const zip = new AdmZip(archive);
			for (const entry of zip.getEntries()) {
				const entryName = entry.entryName;
				if (!entryName || entryName.includes('..')) {
					throw new Error('archive entry has forbidden name');
				}
				const destination = path.join(targetDir, entryName);
				const normalized = path.normalize(destination);
				if (!normalized.startsWith(targetDir)) {
					throw new Error('archive entry escapes target directory');
				}
				if (entry.isDirectory) {
					await mkdir(normalized, { recursive: true });
				} else {
					await mkdir(path.dirname(normalized), { recursive: true });
					const data = entry.getData();
					await writeFile(normalized, data);
				}
			}

			if (options.saveUpload ?? true) {
				const uploadsDir = path.join(this.options.workdir, relativeBase, 'uploads');
				await mkdir(uploadsDir, { recursive: true });
				const timestamp = Date.now();
				const safeName = name.replace(/[^a-zA-Z0-9._-]/g, '_');
				const uploadPath = path.join(uploadsDir, `${timestamp}_${safeName}.zip`);
				await writeFile(uploadPath, archive);
			}

			await this.stageAll();
			const commitSha = await this.commit(`node:${nodeId} ${kind.slice(0, -1)}:${name} draft`);
			await this.push();
                        return {
                                storedPath: path.join(relativeBase, kind, name),
                                commitSha,
                        };
                });
        }

        async deleteDraft(options: DeleteDraftArgs): Promise<DeleteDraftResult> {
                const { kind, subnetId, name } = options;
                await this.ensureReady();
                return this.mutex.runExclusive(async () => {
                        await this.ensureReady();
                        const started = Date.now();
                        const auditId = randomUUID();
                        const nodesRoot = path.join(this.options.workdir, 'subnets', subnetId, 'nodes');
                        const tombstoneRoot = path.join(this.options.workdir, 'subnets', subnetId, 'tombstones', kind);
                        const keyPrefix = kind === 'skills' ? 'forge:skills' : 'forge:scenarios';

                        const nodes: string[] = [];
                        if (options.allNodes) {
                                if (fs.existsSync(nodesRoot)) {
                                        const entries = await readdir(nodesRoot, { withFileTypes: true });
                                        for (const entry of entries) {
                                                if (entry.isDirectory()) nodes.push(entry.name);
                                        }
                                }
                        } else {
                                nodes.push(options.nodeId ?? 'hub');
                        }

                        const tombstoneNodes: string[] = [];
                        if (nodes.length === 0 && fs.existsSync(tombstoneRoot)) {
                                const entries = await readdir(tombstoneRoot, { withFileTypes: true });
                                for (const entry of entries) {
                                        if (!entry.isDirectory()) continue;
                                        const candidate = path.join(tombstoneRoot, entry.name, `${name}.json`);
                                        if (fs.existsSync(candidate)) {
                                                tombstoneNodes.push(entry.name);
                                        }
                                }
                        }

                        if (nodes.length === 0 && tombstoneNodes.length === 0) {
                                const error: any = new Error('draft not found');
                                error.code = 'not_found';
                                throw error;
                        }

                        const redisKeyNodes = Array.from(new Set([...nodes, ...tombstoneNodes]));
                        const redisKeys = redisKeyNodes.map((node) => `${keyPrefix}:${subnetId}:${node}:${name}`);
                        const deleted: Array<{ node_id: string; storedPath?: string }> = [];
                        let changed = false;
                        let hadAny = tombstoneNodes.length > 0;

                        for (const node of nodes) {
                                const relativeBase = path.join('subnets', subnetId, 'nodes', node);
                                const storedPath = path.join(relativeBase, kind, name);
                                const targetDir = path.join(this.options.workdir, storedPath);
                                const tombstonePath = path.join(tombstoneRoot, node, `${name}.json`);
                                const exists = fs.existsSync(targetDir);
                                const tombstoneExists = fs.existsSync(tombstonePath);

                                if (!exists && !tombstoneExists) {
                                        continue;
                                }

                                if (exists) {
                                        await rm(targetDir, { recursive: true, force: true });
                                        await mkdir(path.dirname(tombstonePath), { recursive: true });
                                        await writeFile(
                                                tombstonePath,
                                                JSON.stringify({
                                                        auditId,
                                                        deletedAt: new Date().toISOString(),
                                                        kind,
                                                        name,
                                                        node,
                                                }) + '\n',
                                        );
                                        deleted.push({ node_id: node, storedPath });
                                        changed = true;
                                }

                                hadAny = true;
                        }

                        if (!changed && !hadAny) {
                                const error: any = new Error('draft not found');
                                error.code = 'not_found';
                                throw error;
                        }

                        if (changed) {
                                await this.stageAll();
                                const nodesLabel = redisKeyNodes.join(',') || 'none';
                                const commitMessage = `chore(draft): delete ${kind.slice(0, -1)} ${name} nodes:${nodesLabel} [audit:${auditId}]`;
                                await this.commit(commitMessage);
                                await this.push();
                        }

                        console.info('[forge] deleteDraft', {
                                action: 'delete_draft',
                                kind,
                                subnetId,
                                name,
                                nodeId: options.nodeId,
                                allNodes: Boolean(options.allNodes),
                                redisKeys,
                                deleted,
                                auditId,
                                duration_ms: Date.now() - started,
                        });

                        return { deleted: changed ? deleted : [], redisKeys, auditId };
                });
        }

        async deleteRegistry(options: DeleteRegistryArgs): Promise<DeleteRegistryResult> {
                const { kind, subnetId, name, version, allVersions } = options;
                await this.ensureReady();
                return this.mutex.runExclusive(async () => {
                        await this.ensureReady();
                        const started = Date.now();
                        const auditId = randomUUID();
                        const registryRoot = path.join(this.options.workdir, 'subnets', subnetId, 'registry', kind, name);
                        const tombstoneRoot = path.join(
                                this.options.workdir,
                                'subnets',
                                subnetId,
                                'registry',
                                '.tombstones',
                                kind,
                                name,
                        );

                        let versions: string[] = [];
                        if (version) {
                                versions = [version];
                        } else if (allVersions) {
                                if (fs.existsSync(registryRoot)) {
                                        const entries = await readdir(registryRoot, { withFileTypes: true });
                                        versions = entries.filter((entry) => entry.isDirectory()).map((entry) => entry.name);
                                }
                        }

                        const deleted: Array<{ version: string }> = [];
                        const skipped: Array<{ version: string; reason: string }> = [];
                        const tombstoneVersions: string[] = [];
                        let changed = false;

                        if (!versions.length) {
                                const tombstonePath = version
                                        ? path.join(tombstoneRoot, `${version}.json`)
                                        : path.join(tombstoneRoot, '__all__.json');
                                if (fs.existsSync(tombstonePath)) {
                                        console.info('[forge] deleteRegistry idempotent', {
                                                action: 'delete_registry',
                                                kind,
                                                subnetId,
                                                name,
                                                version: version ?? 'all',
                                                auditId,
                                        });
                                        return {
                                                deleted: [],
                                                skipped,
                                                tombstoned: Boolean(allVersions),
                                                auditId,
                                        };
                                }
                                const error: any = new Error('registry artifact not found');
                                error.code = 'not_found';
                                throw error;
                        }

                        for (const ver of versions) {
                                const targetDir = path.join(registryRoot, ver);
                                const tombstonePath = path.join(tombstoneRoot, `${ver}.json`);
                                const exists = fs.existsSync(targetDir);
                                const tombstoneExists = fs.existsSync(tombstonePath);

                                if (!exists) {
                                        if (tombstoneExists) tombstoneVersions.push(ver);
                                        continue;
                                }

                                await rm(targetDir, { recursive: true, force: true });
                                await mkdir(path.dirname(tombstonePath), { recursive: true });
                                await writeFile(
                                        tombstonePath,
                                        JSON.stringify({
                                                auditId,
                                                deletedAt: new Date().toISOString(),
                                                kind,
                                                name,
                                                version: ver,
                                        }) + '\n',
                                );
                                deleted.push({ version: ver });
                                changed = true;
                        }

                        if (!deleted.length && !tombstoneVersions.length) {
                                const error: any = new Error('registry artifact not found');
                                error.code = 'not_found';
                                throw error;
                        }

                        let tombstoned = false;
                        if (allVersions) {
                                let remaining = false;
                                if (fs.existsSync(registryRoot)) {
                                        const entries = await readdir(registryRoot, { withFileTypes: true });
                                        remaining = entries.some((entry) => entry.isDirectory());
                                }
                                if (!remaining) {
                                        tombstoned = true;
                                        const tombstonePath = path.join(tombstoneRoot, '__all__.json');
                                        await mkdir(path.dirname(tombstonePath), { recursive: true });
                                        await writeFile(
                                                tombstonePath,
                                                JSON.stringify({
                                                        auditId,
                                                        deletedAt: new Date().toISOString(),
                                                        kind,
                                                        name,
                                                        allVersions: true,
                                                }) + '\n',
                                        );
                                }
                        }

                        if (changed) {
                                await this.stageAll();
                                const summary = deleted.length === 1
                                        ? deleted[0].version
                                        : allVersions
                                                ? 'all_versions'
                                                : versions.join(',');
                                const commitMessage = `chore(registry): delete ${kind}/${name}@${summary} [audit:${auditId}]`;
                                await this.commit(commitMessage);
                                await this.push();
                        }

                        console.info('[forge] deleteRegistry', {
                                action: 'delete_registry',
                                kind,
                                subnetId,
                                name,
                                version,
                                allVersions: Boolean(allVersions),
                                force: Boolean(options.force),
                                deleted,
                                tombstoneVersions,
                                auditId,
                                duration_ms: Date.now() - started,
                        });

                        return {
                                deleted: changed ? deleted : [],
                                skipped,
                                tombstoned: tombstoned || (allVersions ? tombstoneVersions.length > 0 : false),
                                auditId,
                        };
                });
        }

        /** Если используем GH App — не ставим GIT_SSH_COMMAND (он мешает HTTPS). */
	private gitEnv(useGhApp: boolean): NodeJS.ProcessEnv {
		if (useGhApp) {
			// Полностью наследуем окружение, не подсовывая ssh-команду.
			const env = { ...process.env };
			// На всякий случай уберём «хвосты», если кто-то выставил в хост-среде:
			delete (env as any).GIT_SSH_COMMAND;
			return env;
		}
		if (!this.options.sshKeyPath) {
			return process.env;
		}
		// Аккуратно экранируем путь в кавычках
		const key = this.options.sshKeyPath.includes(' ')
			? `"${this.options.sshKeyPath}"`
			: this.options.sshKeyPath;
		const command = `ssh -i ${key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new`;
		return { ...process.env, GIT_SSH_COMMAND: command };
	}

	private async withFreshOrigin(): Promise<void> {
		const gh = ghEnv();
		if (!(gh.appId && gh.instId && gh.privateKeyPath)) return;
		if (!gh.repo) gh.repo = parseRepoOwnerNameFromUrl(this.options.repoUrl);
		const httpsUrl = await buildHttpsUrlWithToken(gh);
		await ensureOriginUrl(this.git!, httpsUrl);
	}

	private async sync(useGhApp: boolean): Promise<void> {
		if (!this.git) return;
		if (useGhApp) await this.withFreshOrigin();
		await this.git.fetch(['--all']);
		await this.git.reset(['--hard', 'origin/main']);
	}

	private async stageAll(): Promise<void> {
		if (!this.git) throw new Error('forge git is not initialized');
		await this.git.add('.');
	}

	private async commit(message: string): Promise<string> {
		if (!this.git) throw new Error('forge git is not initialized');
		const status = await this.git.status();
		if (status.isClean()) {
			const log = await this.git.log({ n: 1 });
			return log.latest?.hash ?? '';
		}
		const result = await this.git.commit(message);
		return result.commit;
	}

	private async push(): Promise<void> {
		if (!this.git) throw new Error('forge git is not initialized');

		// Точно так же подменим origin на свежий токен перед push (если App)
		const gh = ghEnv();
		const useGhApp = Boolean(gh.appId && gh.instId && gh.privateKeyPath);
		if (useGhApp) {
			if (!gh.repo) gh.repo = parseRepoOwnerNameFromUrl(this.options.repoUrl);
			const httpsUrl = await buildHttpsUrlWithToken(gh);
			await ensureOriginUrl(this.git, httpsUrl);
		}

		await this.git.push('origin', 'HEAD:main');
	}
}
