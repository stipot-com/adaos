import fs from 'node:fs'
import path from 'node:path'

export type LogFileEntry = {
	name: string
	rel: string
	bytes: number | null
	mtime_ms: number | null
}

function resolveLogsDir(): string | null {
	const raw =
		(process.env['ADAOS_DEBUG_LOGS_DIR'] || '').trim() ||
		(process.env['DEBUG_LOGS_DIR'] || '').trim() ||
		''
	if (!raw) return null
	return path.resolve(raw)
}

function isWithinDir(parent: string, child: string): boolean {
	const p = path.resolve(parent)
	const c = path.resolve(child)
	if (p === c) return true
	// Ensure boundary: `C:\logs` should not match `C:\logs2`.
	return c.startsWith(p + path.sep)
}

export function getLogsDirOrThrow(): string {
	const dir = resolveLogsDir()
	if (!dir) {
		throw new Error(
			'logs_dir_not_configured: set ADAOS_DEBUG_LOGS_DIR (or DEBUG_LOGS_DIR) inside the backend container'
		)
	}
	return dir
}

export async function listLogFiles(opts?: {
	contains?: string | null
	limit?: number
}): Promise<LogFileEntry[]> {
	const dir = getLogsDirOrThrow()
	const contains = (opts?.contains || '').trim()
	const limit = Math.max(1, Math.min(5000, Math.floor(opts?.limit ?? 500)))

	async function safeStat(full: string): Promise<{ bytes: number | null; mtime_ms: number | null }> {
		try {
			const st = await fs.promises.stat(full)
			return {
				bytes: typeof st.size === 'number' ? st.size : null,
				mtime_ms: st.mtimeMs ? Math.floor(st.mtimeMs) : null,
			}
		} catch {
			return { bytes: null, mtime_ms: null }
		}
	}

	const out: LogFileEntry[] = []
	const stack: Array<{ full: string; rel: string; depth: number }> = [{ full: dir, rel: '.', depth: 0 }]
	const maxDepth = 2

	while (stack.length && out.length < limit) {
		const cur = stack.pop()!
		let entries: fs.Dirent[] = []
		try {
			entries = await fs.promises.readdir(cur.full, { withFileTypes: true })
		} catch {
			continue
		}
		for (const ent of entries) {
			if (out.length >= limit) break
			const name = ent.name
			if (name === '.' || name === '..') continue
			const full = path.join(cur.full, name)
			const rel = cur.rel === '.' ? name : path.join(cur.rel, name)

			if (ent.isDirectory()) {
				if (cur.depth < maxDepth) {
					stack.push({ full, rel, depth: cur.depth + 1 })
				}
				continue
			}
			if (!ent.isFile()) continue
			if (contains && !rel.includes(contains) && !name.includes(contains)) continue

			const st = await safeStat(full)
			out.push({ name, rel: rel.replace(/\\/g, '/'), bytes: st.bytes, mtime_ms: st.mtime_ms })
		}
	}

	out.sort((a, b) => (b.mtime_ms ?? 0) - (a.mtime_ms ?? 0))
	return out
}

export async function tailLogFile(opts: {
	relPath: string
	lines: number
	maxBytes?: number
}): Promise<{ rel: string; bytes_read: number; lines: string[] }> {
	const dir = getLogsDirOrThrow()
	const rel = String(opts.relPath || '').trim().replace(/\\/g, '/')
	if (!rel || rel.includes('\0')) throw new Error('invalid_path')
	if (path.isAbsolute(rel)) throw new Error('absolute_path_not_allowed')
	if (rel.includes('..')) throw new Error('path_traversal_not_allowed')

	const full = path.resolve(dir, rel)
	if (!isWithinDir(dir, full)) throw new Error('path_outside_logs_dir')

	const wantLines = Math.max(1, Math.min(50_000, Math.floor(opts.lines || 500)))
	const maxBytes = Math.max(8_192, Math.min(25_000_000, Math.floor(opts.maxBytes ?? 2_000_000)))

	const fd = await fs.promises.open(full, 'r')
	try {
		const st = await fd.stat()
		const size = typeof st.size === 'number' ? st.size : 0
		let pos = size
		let buf = Buffer.alloc(0)
		let bytesReadTotal = 0

		while (pos > 0 && bytesReadTotal < maxBytes) {
			const chunk = Math.min(64 * 1024, pos, maxBytes - bytesReadTotal)
			pos -= chunk
			const tmp = Buffer.allocUnsafe(chunk)
			const { bytesRead } = await fd.read(tmp, 0, chunk, pos)
			if (bytesRead <= 0) break
			bytesReadTotal += bytesRead
			buf = Buffer.concat([tmp.subarray(0, bytesRead), buf])

			// Count newlines. Stop early if we already have enough.
			let n = 0
			for (let i = buf.length - 1; i >= 0; i--) {
				if (buf[i] === 10) n++
				if (n >= wantLines + 2) break
			}
			if (n >= wantLines + 2) break
		}

		const text = buf.toString('utf8')
		const parts = text.split(/\r?\n/)
		const tail = parts.filter((x) => x !== '').slice(-wantLines)
		return { rel, bytes_read: bytesReadTotal, lines: tail }
	} finally {
		await fd.close()
	}
}

