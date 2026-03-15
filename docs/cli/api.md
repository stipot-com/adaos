# API CLI

`adaos api serve` starts the local FastAPI hub/member API.

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
