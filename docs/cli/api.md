# API CLI

`adaos api serve` starts the local FastAPI hub/member API.

`adaos api serve` remains the developer entrypoint. Production OS autostart uses a separate runner path and should be treated as the long-lived service lifecycle.

`adaos api stop` resolves the active local hub address from `node.yaml` (`hub_url`) and performs a graceful shutdown first.

## Shutdown flow

When you run `adaos api stop`, AdaOS uses this sequence:

1. Read `hub_url` from the active `node.yaml`.
2. Verify that the URL is local and has an explicit `host:port`.
3. Call `POST /api/admin/shutdown` with the local AdaOS token.
4. Inside the server:
   - publish `subnet.stopping` on the local event bus;
   - wait until async event handlers drain;
   - request uvicorn graceful shutdown.
5. During FastAPI lifespan teardown:
   - send `subnet.stopped` to Telegram if Telegram routing is active;
   - publish `subnet.stopped` on the local event bus;
   - wait again for local async handlers to drain;
   - stop router / Yjs / subnet services and finish bootstrap shutdown.
6. If the process does not exit in time, CLI falls back to force termination.

## Event contract

Graceful shutdown emits two local runtime events:

- `subnet.stopping`
- `subnet.stopped`

Payload fields:

- `subnet_id`
- `reason`

These events are intended for in-process skills and scenario runtime components that need to flush state, update UI, or persist telemetry before the node exits.

## Drain mode

`POST /api/admin/drain` marks the node as `draining` without stopping it immediately.

While draining:

- `/health/ready` returns `503`;
- `/api/node/status` exposes `node_state=draining`;
- new `/api/tools/call` requests are rejected with `503`;
- member heartbeats publish `node_state=draining` so the hub can stop selecting that node for routed tool calls.

## Core update flow

Production `autostart` can now participate in a core-update lifecycle:

- `POST /api/admin/update/start` schedules an update countdown;
- `POST /api/admin/update/cancel` aborts the countdown;
- `GET /api/admin/update/status` returns the persisted state and pending plan;
- after countdown, the node writes a pending update plan, enters drain mode, and shuts down gracefully;
- on next `autostart` launch, the runner applies `ADAOS_CORE_UPDATE_CMD` before importing the API server.

`ADAOS_CORE_UPDATE_CMD` is intentionally explicit. AdaOS does not yet assume one deploy layout; the command can point to your repo pull/bootstrap/install script and may use placeholders such as `{target_rev}`, `{target_version}`, `{base_dir}`, `{python}`, and `{repo_root}`.
