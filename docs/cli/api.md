# API Service

## Start and stop

```bash
adaos api serve --host 127.0.0.1 --port 8777
adaos api stop
adaos api restart
```

The API command manages a local FastAPI process and keeps a pidfile in runtime state. On restart and stop it attempts graceful shutdown first, then falls back to process termination when necessary.

## Health and status

Unauthenticated endpoints:

- `GET /health/live`
- `GET /health/ready`

Authenticated examples:

- `GET /api/status`
- `GET /api/services`
- `GET /api/node/status`
- `GET /api/observe/stream`

## Authentication

Most operational endpoints require:

```http
X-AdaOS-Token: <token>
```

The CLI resolves this token automatically for local control operations.

## Major API areas

- `/api/services/*`: service skill supervision
- `/api/skills/*`: skill listing, install/update, runtime prepare/activate, uninstall
- `/api/scenarios/*`: scenario install, sync, push, uninstall
- `/api/node/*`: node status, reliability, control-plane projections, role, members, media, Yjs webspaces
- `/api/observe/*`: ingest, tail, and SSE stream
- `/api/subnet/*`: register, heartbeat, context, nodes, deregister
- `/api/admin/*`: drain, shutdown, lifecycle, and core-update orchestration
- `/v1/root/*`: root bootstrap, join-code flows, and Root MCP Foundation skeleton

Current control-plane projection facades under `/api/node/*` are intentionally narrow and aggregate-focused:

- `GET /api/node/control-plane/objects/self`
- `GET /api/node/control-plane/projections/reliability`
- `GET /api/node/control-plane/projections/inventory`
- `GET /api/node/control-plane/projections/neighborhood`

Current Root MCP Foundation skeleton endpoints are exposed separately from `/api/node/*`:

- `GET /v1/root/mcp/foundation`
- `GET /v1/root/mcp/contracts`
- `GET /v1/root/mcp/descriptors`
- `GET /v1/root/mcp/descriptors/{descriptor_id}`
- `GET /v1/root/mcp/targets`
- `GET /v1/root/mcp/targets/{target_id}`
- `POST /v1/root/mcp/targets`
- `POST /v1/root/mcp/access-tokens`
- `POST /v1/root/mcp/call`
- `GET /v1/root/mcp/audit`
- `POST /v1/hub/control/report`
- `GET /v1/hubs/control/reports`

The current skeleton also applies root-side capability checks and scope hints:

- read-only bearer access is allowed only for the default development/registry/audit capabilities
- target lists and tool calls can be scoped by `X-AdaOS-Subnet-Id` and `X-AdaOS-Zone`
- root can issue bounded MCP access tokens for external clients, and those tokens carry inherited `subnet_id` / `zone` / target allowlists
- target-bound `hub.*` tools are now also gated by the managed target's published `infra_access_skill` capability surface
- hub control reports may be verified with `X-AdaOS-Hub-Report-Token` when `ADAOS_ROOT_HUB_REPORT_TOKEN` is configured on root
- hub control reports can refresh managed-target state on `root`, which now powers executable read-side operational tools such as `hub.get_status` and `hub.get_runtime_summary`
- `hub.get_logs` and `hub.run_healthchecks` now work as a local-pilot path only when the target publishes `execution_mode=local_process`
- write-like operational tools are still blocked until the target-side `infra_access_skill` path is implemented

## Positioning Relative to Root MCP Foundation

The current FastAPI surface is a local runtime and node-control API. It is not the future `Root MCP Foundation`.

Phase 0 architectural positioning is:

- local HTTP remains useful for runtime, browser, CLI, and node operations
- broad agent-facing MCP should be introduced on `root`, not by expanding every node into an open infrastructure endpoint
- operational access to managed targets should prefer skill-mediated surfaces such as future `infra_access_skill`
- root MCP should publish root-curated descriptors and managed-target contracts, not expose a direct public SDK bridge
- external MCP clients should be able to target `root` with scoped config such as `root_url`, `subnet_id`, `access_token`, and `zone`
- current `/api/node/control-plane/*` endpoints should stay aggregate-focused and compatible with SDK-first control-plane contracts

This keeps AdaOS from conflating:

- local node API
- human-facing web control surfaces
- future root-hosted agent-facing MCP surfaces

## Notes

- `/api/say` and `/api/io/console/print` still exist, but the code marks them as deprecated in favor of bus-driven flows.
- The API server also mounts additional routers for join flows, STT, NLU teacher functions, and external IO webhooks.
