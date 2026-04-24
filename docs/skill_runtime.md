# Skill Runtime Lifecycle

AdaOS provisions an isolated runtime per skill with versioned A/B slots. Each installation produces a fully self-contained copy of the skill sources, dependencies, resolved manifest, and metadata that can be activated atomically.

## Directory layout

Every skill lives under `skills/<name>` in the workspace. Runtime artefacts are stored separately:

```
skills/.runtime/<name>/<version>/
    slots/<A|B>/
        src/                    # snapshot of the skill sources
        env/                    # shared site-packages for current interpreter
        venv/                   # isolated interpreter (if requested)
        node_modules/
        bin/
        cache/
        logs/
        tmp/
        resolved.manifest.json
    active                      # marker with the current slot (A or B)
    previous                    # marker with the last healthy slot
    meta.json                   # test results, timestamps, history
skills/.runtime/<name>/data/
    db/
    files/
        secrets.json            # per-skill secrets (managed via CLI/setup)
        .skill_env.json         # persisted environment snapshot
    internal/
        a/                      # optional internal data slot
        b/                      # optional internal data slot
        active                  # current internal data slot marker
        previous                # previous internal data slot marker
```

All paths are relative and compatible with Linux/Windows. Slots are created lazily during `adaos skill install`; both directories always exist so an operator can switch immediately.

## Install → test → activate

`adaos skill install <name>` performs the pipeline below:

1. Select the inactive slot (A/B) for the target version and wipe any previous contents.
2. Copy the current contents of `skills/<name>` into `slots/<slot>/src`.
3. Build runtime dependencies (either reusing the host interpreter or creating an isolated virtualenv).
4. Enrich `manifest.json` into `resolved.manifest.json`, resolving tool entry points, interpreter paths, timeouts, and policy defaults.
5. Prepare `data/internal/<a|b>` for the target slot. By default AdaOS copies the current internal slot into the inactive one. If the skill declares a custom `data_migration_tool`, AdaOS invokes that tool instead.
6. Optionally run `src/skills/<name>/tests/` (`--test`) from the prepared slot. Commands execute inside the staged environment (interpreter, `PYTHONPATH`, `.skill_env.json`), and logs are streamed to `slots/<slot>/logs/tests.log`.
7. Persist slot metadata (tests, timestamps, default tool, data migration result) for status and rollback operations.

`adaos skill activate <name>` switches the `active` marker to the prepared slot atomically, records the previous slot for `adaos skill rollback`, writes the active version marker, and also switches `data/internal/active` to the corresponding `a|b` slot. Setup flows must run **after activation** so that secrets and runtime paths are stable.

`adaos skill rollback <name>` rolls back both the runtime slot and the `data/internal/active` pointer.

Important architectural note:

- activation is a slot-pointer switch, not a generic live-memory migration
- in-process skills typically pick up new code on the next invocation from the active slot
- service skills are explicitly restarted by the runtime lifecycle
- durable migration authority belongs to persisted state and `data/internal/<a|b>`, while derived caches/projections should be rebuilt after activation

For the target kernel-facing migration architecture, including rehydrate and rollback semantics for stateful skills, see [AdaOS Supervisor](architecture/adaos-supervisor.md#skill-runtime-migration-lifecycle).

## Deactivate lifecycle

AdaOS may keep the core switch committed while quarantining a subset of skills.
For that case the runtime lifecycle now includes explicit deactivation:

- a deactivated skill remains installed
- its prepared slot and metadata remain inspectable
- tool execution is blocked with a clear `skill is deactivated` error
- ordinary `activate` clears the deactivation marker and returns the skill to service

This is intended for post-commit checks where rolling back the whole core is unnecessary, but continuing to serve a broken skill would be unsafe.
Core-update orchestration may trigger this automatically after a successful runtime switch if post-commit skill checks fail.
When that happens, the deactivation record now persists the failure contract itself, including `failure_kind`, `failed_stage`, `source`, and whether the core switch was already committed.

## Optional internal data migration

This feature is optional. A skill can ignore `data/internal/a|b` completely and continue using only:

- `data/db/skill_env.json`
- `data/files/*`

Use `data/internal/a|b` only for state that must evolve together with runtime schema changes.

### Default behavior

If a skill does not declare a migration hook, AdaOS simply copies the currently active internal data slot into the inactive one during `prepare_runtime`.

### Custom migration hook

A skill may declare an optional migration hook in its manifest:

```yaml
data_migration_tool: migrate_data
tools:
  - name: migrate_data
    entry: handlers.main:migrate_data
```

The hook is resolved through the same tool system as ordinary skill tools.
During prepare, AdaOS runs it against the staged skill sources for the target slot.

The hook receives a payload with:

- `source_internal_slot`
- `target_internal_slot`
- `source_internal_dir`
- `target_internal_dir`
- `data_root`
- `internal_root`
- `runtime_slot`
- `version`

AdaOS also exposes convenience environment variables while the hook runs:

- `ADAOS_SKILL_INTERNAL_DATA_ROOT`
- `ADAOS_SKILL_INTERNAL_ACTIVE_PATH`
- `ADAOS_SKILL_INTERNAL_TARGET_PATH`

Important notes:

- the hook is optional
- if the hook is absent, AdaOS falls back to copy
- the hook is expected to populate `target_internal_dir`
- on migration failure, AdaOS clears the target internal slot and fails `prepare_runtime`

### Target direction

The target AdaOS migration model separates state classes:

- canonical durable state:
  must survive restart, rollback, and rebuild
- slot-bound schema state:
  belongs under `data/internal/a|b`
- derived runtime state:
  caches, indexes, projections, and similar rebuildable material
- live memory:
  in-flight objects and subscriptions that should be drained and recreated, not migrated implicitly

This means `data_migration_tool` should be used for schema-sensitive persisted state, not as a platform promise that arbitrary process memory can be moved across activation.

After activation, stateful skills are expected to rebuild derived runtime state from durable truth.

### Runtime lifecycle hooks

AdaOS now supports optional lifecycle hooks in the resolved skill manifest.

Preferred declaration shape:

```yaml
lifecycle:
  persist_before_switch: persist_state
  after_activate: after_activate
  rehydrate: rehydrate
  drain: drain
  dispose: dispose
  before_deactivate: before_deactivate
```

The hook names resolve through the ordinary skill `tools` table, just like `data_migration_tool`.

Current behavior:

- `persist_before_switch` runs against the currently active slot before pointer cutover when an active prepared runtime exists
- `after_activate` runs after the new slot becomes active
- `rehydrate` runs after activation to rebuild derived runtime state
- `drain` runs before rollback/deactivate or activation-failure cleanup when declared
- `dispose` runs after `drain` and before `before_deactivate` when declared
- `before_deactivate` runs before explicit deactivate or rollback of the current slot
- global runtime drain now reuses the same contract:
  `subnet.draining` triggers active-skill `drain`, and `subnet.stopping` triggers `dispose` then `before_deactivate` as best-effort shutdown hooks for active installed runtimes

Lifecycle diagnostics are persisted into slot metadata and surfaced by `adaos skill status --json` through `runtime_status().lifecycle`.

If activation already switched to a new version/slot and `rehydrate` then fails, AdaOS now attempts to restore:

- the previous active version marker
- the previous active slot selection
- the previous internal data slot marker
- the previous deactivation state

The failed target slot keeps its lifecycle diagnostics so operators can inspect the failed `rehydrate`, shutdown hooks, and rollback result.

Post-commit migration checks now also consume these lifecycle diagnostics.
That means a skill may be marked failed or selectively deactivated because `rehydrate` / `healthcheck` is already unhealthy even before any explicit post-commit test suite runs.

Operator-facing migration reports also surface lifecycle failures separately from test failures, so a `lifecycle/rehydrate` failure is visible as a first-class shutdown/migration issue rather than only as a generic failed skill.
This same metadata is also written into the deactivation marker when a skill is selectively quarantined after a committed core switch.

## Tool execution and setup

`adaos skill run <name> [<tool>]` reads the active slot’s `resolved.manifest.json`, adds the staged source directory to `sys.path`, and executes the tool callable with per-invocation timeouts. `adaos skill test <name>` reuses the same active slot to execute `src/skills/<name>/tests` without preparing a new build. If a skill declares a `setup` tool it is available via `adaos skill setup <name>` **only after activation**; attempting to run setup while the version is pending reports a clear error instructing the operator to activate first.

## Secrets management

Secrets are stored under `skills/.runtime/<name>/data/files/secrets.json` and are never copied into the source tree. Runtime execution injects secrets at process start and keeps placeholders (`${secret:NAME}`) inside `resolved.manifest.json`.

Use the CLI to manage secrets either globally or per skill:

```
adaos secrets set WEATHER_API_KEY <value> --skill weather_skill
adaos secrets list                      # lists all skills with stored secrets
adaos secrets export > backup.json      # exports secrets grouped by skill (values redacted)
adaos secrets import backup.json        # restores secrets into installed skills
adaos secrets list --skill weather_skill
adaos secrets export --skill weather_skill --show
adaos secrets import dump.json --skill weather_skill
```

`adaos skill setup weather_skill` is a thin wrapper around the skill-defined setup tool that typically requests credentials and persists them via the per-skill secrets backend.

## Observability

Every install/test/activate/run operation logs under `slots/<slot>/logs/`. `adaos skill status --json` surfaces runtime state (active version/slot/readiness/tests). For progress checks:

- Workspace: `adaos skill status <NAME> --fetch --diff` compares `skills/<NAME>` against the workspace registry remote (`adaos-registry.git` main).
- Dev: `adaos skill status --space dev <NAME> --fetch --diff` compares the dev folder against the hub draft state via Root API (requires hub mTLS keys from the bootstrap `node.yaml`).

## Weather skill reference

`.adaos/skills/weather_skill/` demonstrates the complete lifecycle:

1. Install the reference skill with tests: `adaos skill install weather_skill --test`.
2. Activate the freshly prepared slot: `adaos skill activate weather_skill`.
3. Run setup to capture the API key via secrets: `adaos skill setup weather_skill`.
4. Execute the default tool: `adaos skill run weather_skill --json '{"city": "Paris"}'`.

The repository contains smoke and contract tests under `src/skills/<name>/tests/` and an optional health probe that can be used by the platform for readiness checks.
