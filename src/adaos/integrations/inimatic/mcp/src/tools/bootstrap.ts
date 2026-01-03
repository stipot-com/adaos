import { httpJson, rootAuthHeader } from '../http.js';

export async function inimaticBootstrapToken(meta?: Record<string, unknown>) {
  return httpJson('POST', '/v1/bootstrap_token', {
    headers: rootAuthHeader(),
    body: meta ?? {}
  });
}
