import { mkdir, writeFile } from 'node:fs/promises'
import { join, basename } from 'node:path'
import { request } from 'undici'
import ffmpeg from 'fluent-ffmpeg'

const FILES_TMP_DIR = process.env['FILES_TMP_DIR'] || '/tmp'

const apiUrl = (token: string, method: string) => `https://api.telegram.org/bot${token}/${method}`

export async function getFilePath(token: string, file_id: string): Promise<string | { file_path: string; file_size?: number } | null> {
  const { body } = await request(apiUrl(token, 'getFile') + `?file_id=${encodeURIComponent(file_id)}`)
  const json = await body.json() as any
  if (!json?.ok) return null
  const file_path = json.result?.file_path
  const file_size = json.result?.file_size
  if (!file_path) return null
  return { file_path, file_size }
}

export async function downloadFile(token: string, file_path: string, bot_id: string): Promise<string> {
  const destDir = join(FILES_TMP_DIR, 'telegram', bot_id)
  await mkdir(destDir, { recursive: true })
  const url = `https://api.telegram.org/file/bot${token}/${file_path}`
  const name = basename(file_path)
  const res = await request(url)
  const buf = Buffer.from(await res.body.arrayBuffer())
  const dest = join(destDir, name)
  await writeFile(dest, buf)
  return dest
}

export async function convertOpusToWav16k(src: string): Promise<string | null> {
  const out = src.replace(/\.[^./]+$/, '') + '.wav'
  return new Promise((resolve) => {
    try {
      ffmpeg(src).audioFrequency(16000).audioChannels(1).toFormat('wav').on('end', () => resolve(out)).on('error', () => resolve(null)).save(out)
    } catch {
      resolve(null)
    }
  })
}
