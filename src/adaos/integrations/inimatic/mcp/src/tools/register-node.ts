import { httpJson } from '../http.js';

export async function inimaticRegisterNode(args: {
  bootstrapToken?: string;
  subnetId?: string;
  csrPem: string;
}) {
  if (!args.csrPem) {
    throw new Error('csrPem is required');
  }

  if (args.bootstrapToken) {
    if (!args.subnetId) {
      throw new Error('subnetId is required when using bootstrapToken');
    }

    return httpJson('POST', '/v1/nodes/register', {
      headers: { 'X-Bootstrap-Token': args.bootstrapToken },
      body: {
        subnet_id: args.subnetId,
        csr_pem: args.csrPem
      }
    });
  }

  // mTLS path — backend поддерживает, MCP без client cert по факту не сможет
  return httpJson('POST', '/v1/nodes/register', {
    body: { csr_pem: args.csrPem }
  });
}
