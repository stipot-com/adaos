import { request, Agent, type Dispatcher } from 'undici';

import { BACKEND_URL, ROOT_TOKEN, TLS_INSECURE } from './env.js';
import { mapHttpError, mapNetworkError } from './errors.js';

type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE' | 'HEAD' | 'OPTIONS';

const agent: Dispatcher | undefined = TLS_INSECURE
  ? new Agent({ connect: { rejectUnauthorized: false } })
  : undefined;

export async function httpJson(
  method: HttpMethod,
  path: string,
  options: {
    headers?: Record<string, string>;
    body?: unknown;
  } = {}
): Promise<any> {
  try {
    const url = new URL(path, BACKEND_URL);

    const res = await request(url, {
      method,
      headers: {
        ...(options.body !== undefined ? { 'content-type': 'application/json' } : {}),
        ...(options.headers ?? {}),
      },
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      dispatcher: agent,
    });

    const text = await res.body.text();

    let data: any = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }

    if (res.statusCode < 200 || res.statusCode >= 300) {
      mapHttpError(res.statusCode, data);
    }

    return data;
  } catch (e) {
    mapNetworkError(e);
  }
}

export function rootAuthHeader(): Record<string, string> {
  if (!ROOT_TOKEN) {
    throw new Error('INIMATIC_ROOT_TOKEN is required for this operation');
  }
  return { 'X-Root-Token': ROOT_TOKEN };
}
