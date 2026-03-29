import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

import { inimaticHealth } from './tools/health.js';
import { inimaticBootstrapToken } from './tools/bootstrap.js';
import { inimaticRegisterSubnet } from './tools/register-subnet.js';
import { inimaticRegisterNode } from './tools/register-node.js';
import { inimaticSendTelegram } from './tools/send-telegram.js';

const server = new McpServer({
  name: 'inimatic-mcp',
  version: '0.1.0',
});

// IMPORTANT: this SDK version expects ZodRawShape (plain object), not z.object(...)

server.tool(
  'inimatic_health',
  'GET /v1/health — backend healthcheck',
  {},
  async () => inimaticHealth()
);

server.tool(
  'inimatic_bootstrap_token',
  'POST /v1/bootstrap_token — issue one-time bootstrap token (requires root token)',
  {
    meta: z.record(z.string(), z.unknown()).optional(),
  },
  async (args) => inimaticBootstrapToken(args.meta)
);

server.tool(
  'inimatic_register_subnet',
  'POST /v1/subnets/register — register subnet with bootstrap token + CSR',
  {
    bootstrapToken: z.string().min(1),
    csrPem: z.string().min(1),
  },
  async (args) =>
    inimaticRegisterSubnet({
      bootstrapToken: args.bootstrapToken,
      csrPem: args.csrPem,
    })
);

server.tool(
  'inimatic_register_node',
  'POST /v1/nodes/register — register node (bootstrap or mTLS path)',
  {
    bootstrapToken: z.string().min(1).optional(),
    subnetId: z.string().min(1).optional(),
    csrPem: z.string().min(1),
  },
  async (args) =>
    inimaticRegisterNode({
      bootstrapToken: args.bootstrapToken,
      subnetId: args.subnetId,
      csrPem: args.csrPem,
    })
);

server.tool(
  'inimatic_send_telegram',
  'POST /io/tg/send — send message via backend Telegram IO',
  {
    text: z.string().min(1),
    hubId: z.string().min(1).optional(),
    chatId: z.string().min(1).optional(),
    botId: z.string().min(1).optional(),
  },
  async (args) =>
    inimaticSendTelegram({
      text: args.text,
      hubId: args.hubId,
      chatId: args.chatId,
      botId: args.botId,
    })
);

const transport = new StdioServerTransport();
await server.connect(transport);
