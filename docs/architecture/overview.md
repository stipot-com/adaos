# Runtime Overview

## Request flow

The usual control path is:

1. `adaos` CLI loads settings, prepares the `AgentContext`, and ensures the local environment exists.
2. Commands either work directly through local services or call the local control API.
3. The API server exposes the same runtime state through FastAPI routers.
4. Services coordinate persistence, git workspaces, runtime slots, event delivery, and external integration behavior.

## Implemented subsystems

- `api server`: health checks, status, shutdown/drain, services, skills, scenarios, node operations, observe stream, STT, join flows
- `node runtime`: role management, hub/member coordination, reliability reporting, member updates, join codes
- `skill runtime`: install, validate, update, activate, rollback, service supervision
- `scenario runtime`: install, validate, run, and test scenarios
- `webspace runtime`: Yjs-backed webspaces, desktop state, scenario switching, home scenario control
- `developer workflows`: Root login/bootstrap, Forge-style push and publish commands

## Storage and state

AdaOS stores most local state under the active base directory, including:

- workspace copies for skills and scenarios
- SQLite registry data
- runtime state and logs
- Yjs and webspace state
- service diagnostics and doctor reports

## Networking model

The repository currently assumes a local control API plus optional subnet communication:

- local API uses `X-AdaOS-Token`
- hub/member coordination uses node config plus subnet endpoints
- join-code onboarding is available through hub and API flows
- some features integrate with Root-hosted infrastructure when configured
