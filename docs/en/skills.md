# Skills

A **skill** is a reusable, versioned unit of functionality that exposes tools and/or event handlers to the AdaOS runtime.
At the filesystem level, each skill lives in its own directory with a manifest (`skill.yaml`) and a handler module
(`handlers/main.py`).

---

## Directory layout

Minimal layout for a skill in the workspace:

```text
skills/
  <skill-name>/
    handlers/
      main.py          # skill handlers (event subscribers, tools)
    i18n/
      ru.json          # localised strings (optional)
      en.json
    prep/
      prep_prompt.md   # preparation request to LLM (optional)
      prepare.py       # preparation code (optional)
    tests/
      conftest.py      # tests (optional)
    .skill_env.json    # skill-specific env defaults (optional)
    config.json        # skill configuration (optional)
    prep_result.json   # preparation result (optional)
    skill_prompt.md    # code-generation request to LLM (optional)
    skill.yaml         # skill manifest (required)
```

Runtime slots (`.adaos/workspace/skills/.runtime/...`) are maintained by the platform and usually do not need to be edited
manually; they are derived from the workspace skills via `adaos dev skill activate` or the runtime updater.

---

## Manifest (`skill.yaml`)

The `skill.yaml` manifest is the single source of truth for skill metadata. It is validated by:

- `src/adaos/abi/skill.schema.json` – public ABI schema for IDEs and LLM tools.
- `src/adaos/services/skill/skill_schema.json` – internal validator used by the skill validator and runtime.

### Minimal example

```yaml
name: weather_skill
version: 2.0.0
description: Simple weather demo skill that shows current conditions on the desktop and reacts to city changes.
runtime:
  python: "3.11"
dependencies:
  - requests>=2.31
events:
  subscribe:
    - "nlp.intent.weather.get"
    - "weather.city_changed"
  publish:
    - "ui.notify"
default_tool: get_weather
tools:
  - name: get_weather
    description: Resolve current weather for the requested city and return a basic summary for the UI.
    entry: handlers.main:get_weather
    input_schema:
      type: object
      required: [city]
      properties:
        city: { type: string, minLength: 1 }
    output_schema:
      type: object
      required: [ok]
      properties:
        ok: { type: boolean }
        city: { type: string }
        temp: { type: number }
        description: { type: string }
        error: { type: string }
```

### Key fields

- `name` – stable skill identifier, used in capacity (`node.yaml`), SDK, and CLI (`adaos dev skill ...`).
- `version` – semantic version of the skill package (see `src/adaos/abi/skill.schema.json` for exact pattern).
- `description` – human‑readable summary, used in UIs and for LLM programmers.
- `runtime` – runtime requirements (currently primarily `python` version).
- `dependencies` – runtime package dependencies (usually pip requirement strings).
- `events` – static hints about subscribed and published topics; real subscriptions are declared via
  `@subscribe` in `handlers/main.py`.
- `tools` – public tools exposed by the skill; each entry should correspond to an `@tool` in `handlers/main.py` and
  declare `input_schema`/`output_schema` that match the implementation.
- `default_tool` – optional default tool name used by the control plane and some SDK helpers.
- `exports.tools` – optional explicit list of tools that should be visible to external callers and LLM agents.

---

## Running a skill

CLI:

```bash
adaos skill run weather_skill
adaos skill run weather_skill --topic nlp.intent.weather.get --payload '{"city": "Berlin"}'
```

From Python:

```python
from adaos.services.skill.runtime import run_skill_handler_sync

result = run_skill_handler_sync(
    "weather_skill",
    "nlp.intent.weather.get",
    {"city": "Berlin"},
)
print(result)
```

For dev/testing scenarios, prefer the higher-level SDK helpers under `adaos.sdk.manage.skills.*` and `adaos.sdk.skills.testing`.

---

## Repository layouts

### Monorepo

- All skills live in a single Git repo under `skills/`.
- Good for tight coupling, shared tooling and CI.

### One-repo-per-skill

- Each skill has its own repository; a skill registry is used to pull them into the node.
- Better for independent versioning and ownership.

AdaOS supports both models via the `SkillRepository` abstraction and the skill registry (`SqliteSkillRegistry` /
Git-backed repositories).

---

## Best practices

- Keep `skill.yaml` small, well‑commented and in English – this is the primary contract for LLM programmers.
- Use `adaos.sdk.core.decorators.subscribe` for event handlers and `@tool` for tools; avoid ad‑hoc entrypoints.
- Store secrets via `adaos.sdk.data.secrets` or the configured secrets backend, never in manifests.
- Put tests under `skills/<name>/tests` and run them regularly via `adaos tests run`.
- For dev skills, use the `adaos dev skill ...` commands and the Prompt IDE (`prompt_engineer_scenario`) to manage
  versions and Git workflows.

