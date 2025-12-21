import express from 'express'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import YAML from 'yaml'
import { buildInfo } from '../../build-info.js'
import fs from 'node:fs'

const DEFAULT_CODEMAP_PATHS = [
  'artifacts/code_map.yaml',
  'artifacts/code_map.yml',
  'artifacts/code_map.json',
  'code_map.yaml',
  'code_map.yml',
  'code_map.json',
]

// Ищем корень репо строго по наличию artifacts/code_map.*
function findRepoRootByCodeMap(startDir: string): { repoRoot: string | null; scanned: string[] } {
  let dir = startDir
  const scanned: string[] = []

  for (let i = 0; i < 80; i++) {
    scanned.push(dir)

    const artifactsDir = path.join(dir, 'artifacts')
    if (fs.existsSync(artifactsDir)) {
      const hasMap =
        fs.existsSync(path.join(artifactsDir, 'code_map.yaml')) ||
        fs.existsSync(path.join(artifactsDir, 'code_map.yml')) ||
        fs.existsSync(path.join(artifactsDir, 'code_map.json'))
      if (hasMap) return { repoRoot: dir, scanned }
    }

    const parent = path.dirname(dir)
    if (parent === dir) break
    dir = parent
  }

  return { repoRoot: null, scanned }
}

function getRepoRoot(): { repoRoot: string; scanned: string[] } {
  const currentDir = path.dirname(fileURLToPath(import.meta.url))

  // 1) Самый надёжный способ — найти реальный корень по artifacts/code_map.*
  const byMap = findRepoRootByCodeMap(currentDir)
  if (byMap.repoRoot) return { repoRoot: byMap.repoRoot, scanned: byMap.scanned }

  // 2) Фолбек: try .git
  // (иногда artifacts может отсутствовать в dev окружении)
  let dir = currentDir
  for (let i = 0; i < 80; i++) {
    if (fs.existsSync(path.join(dir, '.git'))) {
      return { repoRoot: dir, scanned: [...byMap.scanned, dir] }
    }
    const parent = path.dirname(dir)
    if (parent === dir) break
    dir = parent
  }

  // 3) Последний фолбек — CWD (хотя он часто "backend", поэтому не идеален)
  return { repoRoot: process.cwd(), scanned: byMap.scanned }
}

async function readFirstExisting(repoRoot: string, relativePaths: string[]) {
  const triedAbs: string[] = []

  for (const rel of relativePaths) {
    const abs = path.resolve(repoRoot, rel)
    triedAbs.push(abs)
    try {
      const text = await readFile(abs, 'utf8')
      return { rel, abs, text, triedAbs }
    } catch {
      // continue
    }
  }

  return { hit: null as null, triedAbs }
}

function isTextFileName(p: string): boolean {
  const lower = p.toLowerCase()
  return (
    lower.endsWith('.ts') ||
    lower.endsWith('.tsx') ||
    lower.endsWith('.js') ||
    lower.endsWith('.mjs') ||
    lower.endsWith('.cjs') ||
    lower.endsWith('.json') ||
    lower.endsWith('.yaml') ||
    lower.endsWith('.yml') ||
    lower.endsWith('.md') ||
    lower.endsWith('.txt')
  )
}

function assertSafeRelPath(rel: string) {
  const normalized = rel.replace(/\\/g, '/').trim()

  if (!normalized || normalized.startsWith('/') || normalized.includes('\0')) {
    throw Object.assign(new Error('invalid_path'), { code: 'invalid_path' })
  }
  if (normalized.includes('..')) {
    throw Object.assign(new Error('invalid_path'), { code: 'invalid_path' })
  }
  if (normalized.startsWith('.')) {
    throw Object.assign(new Error('hidden_path_forbidden'), { code: 'hidden_path_forbidden' })
  }

  const denyPrefixes = ['.git/', 'node_modules/', 'dist/', 'build/', 'ssl/']
  for (const pref of denyPrefixes) {
    if (normalized.startsWith(pref)) {
      throw Object.assign(new Error('path_forbidden'), { code: 'path_forbidden' })
    }
  }
}

export function installMetaRoutes(
  router: express.Router,
  opts: {
    respondError: (req: express.Request, res: express.Response, status: number, code: string, params?: any) => void
  }
) {
  const { respondError } = opts

  const { repoRoot, scanned } = getRepoRoot()

  router.get('/meta', (req, res) => {
    const debug = String(req.query['debug'] || '') === '1'
    res.json({
      ok: true,
      version: buildInfo.version,
      build_date: buildInfo.buildDate,
      commit: buildInfo.commit,
      // по умолчанию не светим пути
      ...(debug ? { debug_repo_root: repoRoot, debug_scanned_dirs_count: scanned.length } : {}),
    })
  })

  router.get('/meta/code-map', async (req, res) => {
    const fmt = String(req.query['format'] || '').toLowerCase()
    const debug = String(req.query['debug'] || '') === '1'

    // ВАЖНО: если хотите — можно задать абсолютный путь env-переменной и вообще не гадать
    // CODE_MAP_ABS=C:\Users\Danil\Documents\GitHub\MCP\artifacts\code_map.yaml
    const codeMapAbs = process.env['CODE_MAP_ABS']
    if (codeMapAbs && codeMapAbs.trim()) {
      try {
        const text = await readFile(codeMapAbs.trim(), 'utf8')
        if (fmt === 'yaml' || codeMapAbs.toLowerCase().endsWith('.yml') || codeMapAbs.toLowerCase().endsWith('.yaml')) {
          return res.type('text/yaml').send(text)
        }
        try {
          const obj = codeMapAbs.toLowerCase().endsWith('.json') ? JSON.parse(text) : YAML.parse(text)
          return res.json({ ok: true, source: codeMapAbs.trim(), map: obj })
        } catch {
          return res.type('text/plain').send(text)
        }
      } catch (e) {
        if (debug) {
          return res.status(500).json({ ok: false, error: 'CODE_MAP_ABS_read_failed', code_map_abs: codeMapAbs })
        }
        // если env задан, но не читается — это должно быть заметно
        return respondError(req, res, 500, 'internal_error')
      }
    }

    const result = await readFirstExisting(repoRoot, DEFAULT_CODEMAP_PATHS)

    // не найдено
    if (!('rel' in (result as any))) {
      if (debug) {
        return res.status(404).json({
          ok: false,
          error: 'not_found',
          debug_repo_root: repoRoot,
          debug_tried: result.triedAbs,
        })
      }
      return respondError(req, res, 404, 'not_found')
    }

    const hit = result as { rel: string; abs: string; text: string; triedAbs: string[] }

    if (fmt === 'yaml' || hit.rel.endsWith('.yml') || hit.rel.endsWith('.yaml')) {
      return res.type('text/yaml').send(hit.text)
    }

    try {
      const obj = hit.rel.endsWith('.json') ? JSON.parse(hit.text) : YAML.parse(hit.text)
      return res.json({ ok: true, source: hit.rel, map: obj })
    } catch {
      return res.type('text/plain').send(hit.text)
    }
  })

  router.get('/meta/source', async (req, res) => {
    const rel = String(req.query['path'] || '').trim()
    if (!rel) return respondError(req, res, 400, 'missing_params')

    try {
      assertSafeRelPath(rel)
      if (!isTextFileName(rel)) return respondError(req, res, 415, 'unsupported_media_type')

      const abs = path.resolve(repoRoot, rel)

      // гарантия, что abs внутри repoRoot
      const relToRoot = path.relative(repoRoot, abs).replace(/\\/g, '/')
      if (!relToRoot || relToRoot.startsWith('..') || path.isAbsolute(relToRoot)) {
        return respondError(req, res, 403, 'unauthorized')
      }

      const text = await readFile(abs, 'utf8')
      const MAX_CHARS = 200_000
      if (text.length > MAX_CHARS) {
        return res.status(413).json({ error: 'too_large', max_chars: MAX_CHARS })
      }

      return res.type('text/plain').send(text)
    } catch (e: any) {
      const code = e?.code
      if (code === 'invalid_path') return respondError(req, res, 400, 'invalid_name')
      if (code === 'hidden_path_forbidden' || code === 'path_forbidden') return respondError(req, res, 403, 'unauthorized')
      return respondError(req, res, 404, 'not_found')
    }
  })
}
