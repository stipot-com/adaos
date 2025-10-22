import IORedis from 'ioredis'

const REDIS_URL = process.env['REDIS_URL'] || `redis://${process.env['PRODUCTION'] ? 'redis' : 'localhost'}:6379`
export const redis = new IORedis(REDIS_URL)

export async function idemGet(key: string) {
  const raw = await redis.get(key)
  if (!raw) return null
  try { return JSON.parse(raw) } catch { return null }
}

export async function idemPut(key: string, value: { status: number; body: any }, ttlSec: number) {
  await redis.set(key, JSON.stringify(value), 'EX', Math.max(1, ttlSec))
}

