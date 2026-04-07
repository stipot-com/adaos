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

Current control-plane projection facades under `/api/node/*` are intentionally narrow and aggregate-focused:

- `GET /api/node/control-plane/objects/self`
- `GET /api/node/control-plane/projections/reliability`
- `GET /api/node/control-plane/projections/inventory`
- `GET /api/node/control-plane/projections/neighborhood`

## Notes

- `/api/say` and `/api/io/console/print` still exist, but the code marks them as deprecated in favor of bus-driven flows.
- The API server also mounts additional routers for join flows, STT, NLU teacher functions, and external IO webhooks.
