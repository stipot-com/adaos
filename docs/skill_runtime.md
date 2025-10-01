# Skill Runtime Lifecycle

AdaOS now provisions a per-skill runtime environment with A/B slots and a resolved manifest.  The directory layout for each skill is:

```
skills/<name>/
skills/.runtime/<name>/<version>/
    slots/A/
    slots/B/
    active
    previous
skills/.runtime/<name>/data/{db,files}/
```

During `adaos skill install` the platform performs the following steps:

1. Prepare the runtime directories (creating both A and B slots).
2. Build the interpreter sandbox (currently Python virtual environments).
3. Enrich `manifest.json` into `resolved.manifest.json` with concrete tool shims.
4. Optionally run smoke/contract tests found under `runtime/tests/`.
5. Persist metadata for activation and status reporting.

Activation is handled with `adaos skill activate`, which performs an atomic switch of the `active` marker to the prepared slot.  Rollbacks rely on the `previous` marker and run in constant time.

The resolved manifest exposes executable commands for each tool, retains secret placeholders, and records applied policy defaults.  Operators can inspect the active state via `adaos skill status --json`.

See `.adaos/skills/weather_skill/` for a working example including runtime tests and a basic health probe.
