# Scenarios

A **scenario** is a declarative description of how AdaOS should behave in a given context: which skills are involved,
how data flows between them, and which UI (if any) should be rendered.

Scenarios are defined by manifests under `scenarios/<id>/scenario.yaml` and, for desktop UIs, a corresponding
`scenario.json` that describes the concrete Yjs-based UI model.

---

## Directory layout

Workspace scenarios are stored under the AdaOS workspace (see also `docs/dev-notes/workspace.md`):

```text
.adaos/workspace/scenarios/
  web_desktop/
    scenario.yaml
    scenario.json      # effective UI seed for web_desktop
  prompt_engineer_scenario/
    scenario.yaml
    scenario.json      # Prompt IDE desktop UI + workflow
  ...
```

The `scenario.yaml` manifest is the human-edited source of truth for identity, dependencies and high-level metadata.
The `scenario.json` file is a richer UI seed that may be generated or edited by tooling.

---

## Manifest (`scenario.yaml`)

The structure of `scenario.yaml` is described by:

- `src/adaos/abi/scenario.schema.json` – public ABI schema for IDEs and LLM tools.

### Minimal examples

#### Web Desktop

```yaml
id: web_desktop
version: 0.0.1
title: Web Desktop
description: Desktop shell scenario that defines the main UI layout, registry and base catalog.
type: desktop
depends:
  - web_desktop_skill
updated_at: "2025-11-14T18:14:36+00:00"
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

#### Prompt IDE

```yaml
id: prompt_engineer_scenario
version: "0.1.0"
title: Prompt IDE
description: Prompt engineering IDE workspace for dev skills and scenarios.
type: desktop
depends:
  - prompt_engineer_skill
updated_at: "2025-11-14T18:14:36+00:00"
```

The detailed Prompt IDE workflow (TZ / TZ addenda) and desktop UI are described in the corresponding
`scenario.json` and consumed by `ScenarioWorkflowRuntime` and the web client.

### Key fields

- `id` – stable scenario identifier (must match the directory name and is used in SDK/API calls).
- `name` – optional internal name; if omitted, `id` is used.
- `title` – human‑readable title for UI and LLM descriptions.
- `type` – scenario type, e.g. `desktop` for web desktop scenarios.
- `version` – semantic version of the scenario manifest.
- `description` – short English description of the scenario purpose.
- `depends` – list of required skills (by `skill.yaml.name`).
- `updated_at` – optional ISO-8601 timestamp for tracking manifest changes.
- `data_projections` – optional rules that map `(scope, slot)` pairs (used by `ctx.*`) to physical storage:
  see `src/adaos/services/scenario/projection_registry.py`.

Low-level execution `steps` and high-level `workflow` are also supported and are mainly used by the SDK/CLI
scenario runtime and the Prompt IDE:

- `workflow.initial_state` / `workflow.states` – high-level workflow used by `ScenarioWorkflowRuntime`.
- `steps[]` – procedural steps executed by `ScenarioRuntime` (see `src/adaos/sdk/scenarios/runtime.py`).

For the current MVP, most desktop scenarios focus on `ui` (in `scenario.json`) and `data_projections`/`depends`
in `scenario.yaml`.

---

## Running scenarios

CLI (procedural scenarios with `steps`):

```bash
adaos scenario run catalog-media
```

Runtime (procedural scenarios):

```python
from adaos.sdk.scenarios.runtime import ScenarioRuntime, ensure_runtime_context
from pathlib import Path

ensure_runtime_context(Path("~/.adaos").expanduser())
ScenarioRuntime().run_from_file("~/.adaos/workspace/scenarios/catalog-media")
```

For desktop scenarios (`type: desktop`), execution is driven by the web client:

- The server loads `scenario.json` and projects `ui`/`data` seeds into Yjs.
- `WebspaceScenarioRuntime` merges skill UI contributions (`webui.json`) and scenario catalog/registry.
- `ScenarioWorkflowRuntime` reads the `workflow` section (for Prompt IDE) and manages the prompt/workflow state.

---

## Best practices

- Keep `scenario.yaml` focused on identity (`id`, `version`, `title`, `depends`) and long-lived contracts such as
  `data_projections` and `workflow`.
- Place concrete desktop UI configuration (`ui.application`, modals, widgets) in `scenario.json`, not in YAML, to
  avoid noisy diffs and keep the manifest readable.
- Use English descriptions and titles – they are consumed by both humans and LLM-based tooling.
- Treat `scenario.schema.json` as the contract: if you add new sections (e.g. for data), extend the schema at the
  same time so IDEs and validators stay in sync.
