export type DebugLogLevel = 'log' | 'info' | 'warn' | 'error'

export interface DebugLogEntry {
  ts: string
  level: DebugLogLevel
  args: any[]
}

const LOG_BUFFER_KEY = '__ADAOS_DEBUG_LOGS__'

const DEBUG_FLAG_KEY = 'adaos.debug'
const VERBOSE_DEBUG_FLAG_KEY = 'adaos.debug.verbose'

function getGlobalScope(): any {
  if (typeof globalThis !== 'undefined') {
    return globalThis as any
  }
  if (typeof window !== 'undefined') {
    return window as any
  }
  if (typeof self !== 'undefined') {
    return self as any
  }
  return {} as any
}

export function isDebugEnabled(): boolean {
  try {
    const g = getGlobalScope()
    return (g.localStorage?.getItem(DEBUG_FLAG_KEY) || '') === '1'
  } catch {
    return false
  }
}

export function isVerboseDebugEnabled(): boolean {
  try {
    const g = getGlobalScope()
    return (g.localStorage?.getItem(VERBOSE_DEBUG_FLAG_KEY) || '') === '1'
  } catch {
    return false
  }
}

export function initDebugConsole(): void {
  const g: any = getGlobalScope()
  const original = {
    log: console.log.bind(console),
    info: console.info.bind(console),
    warn: console.warn.bind(console),
    error: console.error.bind(console),
  }

  if (!g[LOG_BUFFER_KEY]) {
    g[LOG_BUFFER_KEY] = [] as DebugLogEntry[]
  }

  const push = (level: DebugLogLevel, args: any[]) => {
    const entry: DebugLogEntry = {
      ts: new Date().toISOString(),
      level,
      args,
    }
    try {
      const buf: DebugLogEntry[] = g[LOG_BUFFER_KEY]
      buf.push(entry)
      if (buf.length > 1000) {
        buf.splice(0, buf.length - 1000)
      }
    } catch {
      // ignore logging errors
    }
  }

  const echoDebug = isDebugEnabled()

  console.log = (...args: any[]) => {
    push('log', args)
    if (echoDebug) original.log(...args)
  }
  console.info = (...args: any[]) => {
    push('info', args)
    if (echoDebug) original.info(...args)
  }
  console.warn = (...args: any[]) => {
    push('warn', args)
    original.warn(...args)
  }
  console.error = (...args: any[]) => {
    push('error', args)
    original.error(...args)
  }

  g.__ADAOS_DEBUG_CONSOLE__ = {
    original,
    push,
    bufferKey: LOG_BUFFER_KEY,
    debugFlagKey: DEBUG_FLAG_KEY,
    verboseDebugFlagKey: VERBOSE_DEBUG_FLAG_KEY,
  }
}
