# Layers

## Code structure

AdaOS follows a practical layered layout:

- `apps`: executable entry points such as CLI and API
- `services`: orchestration, lifecycle, registries, update flows, and runtime logic
- `sdk`: public modules used by skills and higher-level code
- `adapters`: concrete IO implementations
- `ports`: abstractions for infrastructure-facing concerns
- `domain`: core data types and domain-level helpers

## Why this matters

This split keeps most operational code out of the command layer:

- CLI commands stay thin and mostly delegate
- API routers map HTTP requests onto the same services
- adapters contain platform-specific or external-system logic
- SDK modules expose stable entry points for skills and tooling

## Typical paths

- skill lifecycle: `apps/cli/commands/skill.py` -> `services/skill/*` -> `adapters/db`, `adapters/git`, runtime state
- scenario lifecycle: `apps/cli/commands/scenario.py` -> `services/scenario/*`
- node operations: `apps/cli/commands/node.py` and `apps/api/node_api.py` -> node and subnet services
- local API hosting: `apps/cli/commands/api.py` -> `apps/api/server.py`
