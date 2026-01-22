import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// === ВАЖНО: никаких npx, только node + tsx cli ===
const tsxCli = path.join(
  __dirname,
  "node_modules",
  "tsx",
  "dist",
  "cli.mjs"
);

const serverEntry = path.join(
  __dirname,
  "src",
  "mcp-server.ts"
);

const env = {
  ...process.env,
  INIMATIC_BACKEND_URL: "https://localhost:3030",
  INIMATIC_ROOT_TOKEN: "dev-root-token",
  INIMATIC_TLS_INSECURE: "1",
};

function rpc(id, method, params) {
  return JSON.stringify({
    jsonrpc: "2.0",
    id,
    method,
    params,
  }) + "\n";
}

async function main() {
  const child = spawn(
    process.execPath,           // node.exe
    [tsxCli, serverEntry],      // node tsx/cli.mjs mcp-server.ts
    {
      env,
      stdio: ["pipe", "pipe", "inherit"],
      shell: false,             // КРИТИЧНО
      windowsHide: true,
    }
  );

  child.stdout.setEncoding("utf8");

  child.stdout.on("data", (chunk) => {
    chunk
      .toString()
      .split("\n")
      .filter(Boolean)
      .forEach((line) => {
        console.log("[mcp<-]", line);
      });
  });

  // === MCP handshake ===
  child.stdin.write(rpc(1, "initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "tools-test", version: "0.0.1" },
  }));

  child.stdin.write(
    JSON.stringify({
      jsonrpc: "2.0",
      method: "notifications/initialized",
      params: {},
    }) + "\n"
  );

  // list tools
  child.stdin.write(rpc(2, "tools/list", {}));

  // health
  child.stdin.write(
    rpc(3, "tools/call", {
      name: "inimatic_health",
      arguments: {},
    })
  );

  // bootstrap token
  child.stdin.write(
    rpc(4, "tools/call", {
      name: "inimatic_bootstrap_token",
      arguments: {},
    })
  );

  setTimeout(() => {
    child.kill();
  }, 2000);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
