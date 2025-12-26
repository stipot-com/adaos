import { McpError } from '@modelcontextprotocol/sdk/types.js';

export function mapHttpError(status: number, body: any): never {
  const msg = typeof body === 'string' ? body : JSON.stringify(body);

  // Можно уточнить коды, но базово так:
  if (status === 401 || status === 403) {
    throw new McpError(-32001, msg, { http_status: status }); // Unauthorized-ish (custom)
  }
  if (status >= 400 && status < 500) {
    throw new McpError(-32602, msg, { http_status: status }); // Invalid params/request
  }
  throw new McpError(-32603, msg, { http_status: status }); // Internal error
}

export function mapNetworkError(err: unknown): never {
  throw new McpError(
    -32000,
    err instanceof Error ? err.message : 'network_error'
  );
}
