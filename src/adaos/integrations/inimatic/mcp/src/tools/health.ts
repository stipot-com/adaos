import { httpJson } from '../http.js';

export async function inimaticHealth() {
  return httpJson('GET', '/v1/health');
}
