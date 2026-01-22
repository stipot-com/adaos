import { httpJson } from '../http.js';

export async function inimaticRegisterSubnet(args: {
  bootstrapToken: string;
  csrPem: string;
}) {
  if (!args.bootstrapToken || !args.csrPem) {
    throw new Error('bootstrapToken and csrPem are required');
  }

  return httpJson('POST', '/v1/subnets/register', {
    headers: { 'X-Bootstrap-Token': args.bootstrapToken },
    body: { csr_pem: args.csrPem }
  });
}
