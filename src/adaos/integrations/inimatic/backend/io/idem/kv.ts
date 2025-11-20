// src/adaos/integrations/inimatic/backend/io/idem/kv.ts
import * as IORedis from 'ioredis'

const REDIS_URL =
	process.env['REDIS_URL'] ||
	`redis://${process.env['PRODUCTION'] ? 'redis' : 'localhost'}:6379/0`

export const redis = new (IORedis as any).Redis(REDIS_URL)

export async function idemGet<T = any>(key: string): Promise<T | null> {
	const raw = await redis.get(key)
	if (!raw) return null
	try { return JSON.parse(raw) as T } catch { return null }
}

export async function idemPut(key: string, value: unknown, ttlSec: number) {
	const ttl = Math.max(1, ttlSec | 0)
	await redis.set(key, JSON.stringify(value), 'EX', ttl)
}
