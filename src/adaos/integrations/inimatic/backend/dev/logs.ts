type StreamKind = 'stdout' | 'stderr'

export type CapturedLogLine = {
	ts: number
	stream: StreamKind
	line: string
}

type CaptureState = {
	lines: CapturedLogLine[]
	maxLines: number
	bufOut: string
	bufErr: string
	installed: boolean
}

const state: CaptureState = {
	lines: [],
	maxLines: 50_000,
	bufOut: '',
	bufErr: '',
	installed: false,
}

function pushLine(stream: StreamKind, line: string) {
	const cleaned = String(line ?? '').replace(/\r$/, '')
	if (!cleaned) return
	state.lines.push({ ts: Date.now(), stream, line: cleaned })
	if (state.lines.length > state.maxLines) {
		state.lines.splice(0, state.lines.length - state.maxLines)
	}
}

function ingestChunk(stream: StreamKind, chunk: unknown) {
	let text = ''
	try {
		if (typeof chunk === 'string') text = chunk
		else if (chunk instanceof Uint8Array) text = Buffer.from(chunk).toString('utf8')
		else text = String(chunk)
	} catch {
		text = ''
	}
	if (!text) return

	let buf = stream === 'stdout' ? state.bufOut : state.bufErr
	buf += text

	// Split into lines; keep the last unfinished line in the buffer.
	const parts = buf.split(/\n/)
	for (let i = 0; i < parts.length - 1; i++) {
		pushLine(stream, parts[i])
	}
	buf = parts[parts.length - 1] || ''
	if (stream === 'stdout') state.bufOut = buf
	else state.bufErr = buf
}

export function installRootLogCapture(opts?: { maxLines?: number }) {
	if (state.installed) return
	state.installed = true
	if (typeof opts?.maxLines === 'number' && Number.isFinite(opts.maxLines)) {
		state.maxLines = Math.max(1_000, Math.floor(opts.maxLines))
	}

	const origOut = process.stdout.write.bind(process.stdout)
	const origErr = process.stderr.write.bind(process.stderr)

	;(process.stdout as any).write = (chunk: any, encoding?: any, cb?: any) => {
		try {
			ingestChunk('stdout', chunk)
		} catch {}
		return origOut(chunk, encoding, cb)
	}

	;(process.stderr as any).write = (chunk: any, encoding?: any, cb?: any) => {
		try {
			ingestChunk('stderr', chunk)
		} catch {}
		return origErr(chunk, encoding, cb)
	}

	// pino (via sonic-boom) can write directly to file descriptors using fs.writeSync/write,
	// bypassing process.stdout.write. Capture those as well.
	try {
		// eslint-disable-next-line @typescript-eslint/no-var-requires
		const fs = require('node:fs') as typeof import('node:fs')
		const origWriteSync = fs.writeSync.bind(fs)
		const origWrite = fs.write.bind(fs)
		let inFsHook = false

		;(fs as any).writeSync = (fd: number, buffer: any, ...rest: any[]) => {
			if (!inFsHook) {
				try {
					inFsHook = true
					if (fd === 1) ingestChunk('stdout', buffer)
					else if (fd === 2) ingestChunk('stderr', buffer)
				} catch {
					/* ignore */
				} finally {
					inFsHook = false
				}
			}
			// eslint-disable-next-line @typescript-eslint/no-unsafe-argument
			return (origWriteSync as any)(fd, buffer, ...rest)
		}

		;(fs as any).write = (fd: number, buffer: any, ...rest: any[]) => {
			if (!inFsHook) {
				try {
					inFsHook = true
					if (fd === 1) ingestChunk('stdout', buffer)
					else if (fd === 2) ingestChunk('stderr', buffer)
				} catch {
					/* ignore */
				} finally {
					inFsHook = false
				}
			}
			// eslint-disable-next-line @typescript-eslint/no-unsafe-argument
			return (origWrite as any)(fd, buffer, ...rest)
		}
	} catch {
		// best-effort
	}
}

export function queryRootLogs(opts: {
	sinceMs: number
	limit: number
	contains?: string | null
	hubId?: string | null
}): CapturedLogLine[] {
	const since = Number.isFinite(opts.sinceMs) ? opts.sinceMs : 0
	const limit = Math.max(1, Math.min(50_000, Math.floor(opts.limit || 2_000)))
	const contains = (opts.contains || '').trim()
	const hubId = (opts.hubId || '').trim()

	const out: CapturedLogLine[] = []
	for (let i = state.lines.length - 1; i >= 0; i--) {
		const it = state.lines[i]
		if (it.ts < since) break
		if (hubId && !it.line.includes(hubId)) continue
		if (contains && !it.line.includes(contains)) continue
		out.push(it)
		if (out.length >= limit) break
	}
	out.reverse()
	return out
}
