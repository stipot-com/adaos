# Architecture

AdaOS is built as a local-first runtime with a layered Python codebase and a small control surface:

- the CLI builds and uses a shared `AgentContext`
- the FastAPI server exposes the same runtime over HTTP
- services manage skills, scenarios, node state, Yjs webspaces, and runtime lifecycle
- adapters isolate filesystem, database, git, audio, secret, and integration-specific IO

## Main runtime building blocks

- `src/adaos/apps`: CLI, API server, launchers, and process entry points
- `src/adaos/services`: orchestration and runtime logic
- `src/adaos/sdk`: public helpers for skills, scenarios, data access, and decorators
- `src/adaos/adapters`: filesystem, database, git, audio, secrets, and SDK bridge implementations
- `src/adaos/ports`: contracts for infrastructure-facing behavior
- `src/adaos/domain`: core types and registries

## Runtime model

In the current implementation:

- a node can operate as `hub` or `member`
- the local API exposes node, skill, scenario, observe, subnet, join, and service endpoints
- service-type skills are managed through a supervisor and health-aware status API
- Yjs-backed webspaces provide synchronized scenario and desktop state
- autostart and core-update flows are integrated with the runtime lifecycle

The following pages summarize the implemented architecture rather than the target-state research material.
