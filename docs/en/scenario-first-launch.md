# Scenario first launch and data projections

This note walks through how scenarios are launched for the first time and how `data_projections` connect
conceptual state to physical storage (Yjs, KV, SQL) inside AdaOS.

It complements:

- `docs/scenarios.md` – scenario manifests and desktop UI.
- `src/adaos/services/scenario/projection_registry.py` – how projections are loaded and resolved.

---

## 1. Scenario manifests and first launch

A scenario manifest lives under:

```text
.adaos/workspace/scenarios/<id>/scenario.yaml
```

For example, a simple “greet on boot” procedural scenario might look like:

```yaml
id: greet_on_boot
version: 0.1.0
title: Greet on boot
description: >
  Greets the user on first boot, collects basic profile info and shows quickstart tips.
steps:
  - name: ask_name
    call: io.console.print
    args:
      text: "Hi! What's your name?"
  - name: get_name
    call: io.voice.stt.listen
    save_as: user_name
  - name: greet
    call: io.voice.tts.speak
    args:
      text: "Nice to meet you, ${user_name}!"
```

Such scenarios are executed by the procedural `ScenarioRuntime` (see `src/adaos/sdk/scenarios/runtime.py`) and do not
define any UI; they can be launched from the CLI or from Python.

Desktop scenarios (like `web_desktop` and `prompt_engineer_scenario`) additionally have a `scenario.json` that seeds
the UI and workflow into Yjs, but the identity and data projections still live in `scenario.yaml`.

---

## 2. `data_projections`: connecting slots to storage

The `data_projections` section of `scenario.yaml` describes how logical `(scope, slot)` pairs map to concrete storage
backends. At runtime this is loaded by `ProjectionRegistry` and used by higher-level SDK helpers (for example `ctx.*`)
to route reads/writes.

Example from `web_desktop`:

```yaml
id: web_desktop
version: 0.0.1
title: Web Desktop
description: >
  Desktop shell scenario that defines the main UI layout, registry and base catalog.
type: desktop
depends:
  - web_desktop_skill
data_projections:
  - scope: current_user
    slot: profile.settings
    targets:
      - backend: kv
      - backend: yjs
        path: data/skills/profile/{user_id}/settings
  - scope: subnet
    slot: weather.snapshot
    targets:
      - backend: yjs
        path: data/weather
```

Key concepts:

- `scope` – conceptual “owner” of the data: e.g. `current_user`, `device`, `subnet`, `workspace`.
- `slot` – logical name of the data slot within that scope (`profile.settings`, `weather.snapshot`, etc.).
- `targets` – one or more physical projections for this `(scope, slot)` pair:
  - `backend` – which storage backend to use: `yjs`, `kv`, `sql`.
  - `path` – backend-specific location; for Yjs this is usually a path under `data/...`.
  - `webspace_id`, `table`, `column` – additional fields for more advanced backends (SQL, multi-webspace setups).

`ProjectionRegistry` (`src/adaos/services/scenario/projection_registry.py`) loads `data_projections` from the active
scenario manifest and builds an in-memory map:

- keys: `(scope, slot)` pairs;
- values: `ProjectionRule` objects with a list of `ProjectionTarget` entries.

Higher-level code (for example, `ctx.*` helpers or background observers like the weather observer) can resolve a logical
slot into concrete Yjs/KV paths without hard-coding those paths.

In practical terms:

- The weather observer writes to the `weather.snapshot` slot for the `subnet` scope.
- The `web_desktop` scenario’s `data_projections` ensure that this data ends up in the Yjs document under `data/weather`,
  which the desktop UI reads via `kind: "y"` data sources.

---

## 3. Desktop scenarios, UI and workflows

Desktop scenarios (type `desktop`) combine:

- `scenario.yaml` – identity, dependencies, `data_projections`.
- `scenario.json` – UI seeds (`ui.application`, catalogs, modals) and optional `workflow`.

For example, `web_desktop`:

- Uses `data_projections` to map logical slots to Yjs paths.
- Uses `scenario.json["ui"]` to seed the desktop layout, icons and widgets into the Yjs document.
- `WebspaceScenarioRuntime` (`src/adaos/services/scenario/webspace_runtime.py`) then merges this with skill UI
  contributions (`webui.json`) and the installed apps/widgets for each webspace.

For Prompt IDE (`prompt_engineer_scenario`):

- `scenario.yaml` declares the scenario and its dependency on `prompt_engineer_skill`.
- `scenario.json["workflow"]` defines high-level workflow states (`tz`, `tz_add`) and actions calling
  `prompt_engineer_skill` tools.
- `ScenarioWorkflowRuntime` (`src/adaos/services/scenario/workflow_runtime.py`) reads the `workflow` section and
  projects the current state and next actions into Yjs under `data.prompt.workflow`, which the web UI then renders.

---

## Summary

- `scenario.yaml` is the source of truth for scenario identity, dependencies and data projections.
- `data_projections` translate logical `(scope, slot)` pairs into concrete storage targets and are loaded by
  `ProjectionRegistry`.
- Desktop scenarios add a `scenario.json` UI layer that is projected into Yjs, with `WebspaceScenarioRuntime` and
  `ScenarioWorkflowRuntime` tying everything together at runtime.

