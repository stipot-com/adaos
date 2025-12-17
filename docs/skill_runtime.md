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
```

All paths are relative and compatible with Linux/Windows. Slots are created lazily during `adaos skill install`; both directories always exist so an operator can switch immediately.

## Install → test → activate

`adaos skill install <name>` performs the pipeline below:

1. Select the inactive slot (A/B) for the target version and wipe any previous contents.
2. Copy the current contents of `skills/<name>` into `slots/<slot>/src`.
3. Build runtime dependencies (either reusing the host interpreter or creating an isolated virtualenv).
4. Enrich `manifest.json` into `resolved.manifest.json`, resolving tool entry points, interpreter paths, timeouts, and policy defaults.
5. Optionally run `src/skills/<name>/tests/` (`--test`) from the prepared slot. Commands execute inside the staged environment (interpreter, `PYTHONPATH`, `.skill_env.json`), and logs are streamed to `slots/<slot>/logs/tests.log`.
6. Persist slot metadata (tests, timestamps, default tool) for status and rollback operations.

`adaos skill activate <name>` switches the `active` marker to the prepared slot atomically, records the previous slot for `adaos skill rollback`, and writes the active version marker. Setup flows must run **after activation** so that secrets and runtime paths are stable.

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
- Dev: `adaos skill status --space dev <NAME> --fetch --diff` compares the dev folder against the hub draft state via Root API (requires hub mTLS keys in `node.yaml`).

## Weather skill reference

`.adaos/skills/weather_skill/` demonstrates the complete lifecycle:

1. Install the reference skill with tests: `adaos skill install weather_skill --test`.
2. Activate the freshly prepared slot: `adaos skill activate weather_skill`.
3. Run setup to capture the API key via secrets: `adaos skill setup weather_skill`.
4. Execute the default tool: `adaos skill run weather_skill --json '{"city": "Paris"}'`.

The repository contains smoke and contract tests under `src/skills/<name>/tests/` and an optional health probe that can be used by the platform for readiness checks.
