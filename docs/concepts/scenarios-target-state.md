# Scenario subsystem: target state

This document describes the **target architecture** of the scenario subsystem in AdaOS, starting from the current implementation (`ScenarioManager`, `ScenarioRuntime`, `web_desktop_skill`) and moving towards a core-centric model where:

- scenarios are first-class artifacts,
- skills stay Yjs-agnostic and talk only via `ctx`,
- the hub core owns all projections into Yjs/webspaces.

---

## 1. Scope and goals

### 1.1. Goals

- make **scenario** a first-class entity that:
  - describes *what* happens (procedural flow),
  - describes *how* it is presented (UI model for webspaces),
- keep **skills**:
  - focused on business logic and data,
  - interacting only through `ctx` (subnet/current_user/selected_user),
  - independent from Yjs and web UI structure,
- move the responsibility for:
  - merging scenario UI and skill UI,
  - routing data from `ctx` into Yjs / storage,
  - managing webspaces and their lifecycle  
  into **AdaOS core**, not into a single skill.

### 1.2. Out of scope

- low-level function signatures,
- specific storage schemas (SQL tables, kv prefixes),
- concrete YDoc structure beyond high-level maps (`ui`, `data`, `registry`).

---

## 2. Core entities

### 2.1. Scenario

A scenario is a composition of:

1. **Manifest (`scenario.yaml`)**
   - `id`, `version`, metadata;
   - `depends`: list of required skills;
   - scenario type (e.g. `desktop`, `voice_flow`, `service`);
   - declarative options:
     - webspace behaviour (default scenario for new spaces, allowed types),
     - data projections (mapping from `ctx` slots to data sinks).

2. **Content (`scenario.json`)**
   - `ui.application`:
     - layout / areas,
     - base apps, widgets, modals, actions;
   - `catalog`:
     - base catalog of apps/widgets for this scenario;
   - `registry`:
     - scenario-level modals/widgets registry;
   - `data`:
     - initial data seeds (e.g. `data.weather`, `data.desktop`, etc.).

3. **Procedural flow** (optional)
   - executed via `ScenarioRuntime`:
     - `steps`, `when`, `call`, `set`, `save_as`, etc.
   - used for voice flows, orchestration and admin procedures.

> UI scenario and procedural scenario are two roles of the same artifact; they share manifest and content but are used by different runtimes.

---

### 2.2. Skill

A skill remains:

- code + `skill.yaml`,
- optional web description `webui.json` with:
  - `apps`, `widgets`,
  - `registry.modals`, `registry.widgets`,
  - `contributions` (extension points),
  - `ydoc_defaults` (required YDoc paths and default values).

Skill:

- communicates only via `ctx` (three scopes: `subnet`, `current_user`, `selected_user`),
- does **not** know:
  - what webspaces exist,
  - how Yjs is stored or updated,
  - where exactly its data is rendered.

Yjs projection is handled by core, based on scenario configuration.

---

### 2.3. Webspace & Yjs

A **webspace** is a logical desktop / workspace identified by `webspace_id`.

Each webspace has an associated YDoc with three key maps:

- `ui`:
  - current UI state,
  - scenario mapping (`ui.scenarios`),
  - `ui.current_scenario`,
- `data`:
  - catalog (`data.catalog`),
  - installed items (`data.installed`, `data.desktop.installed`),
  - scenario data (`data.scenarios[scenario_id]`),
  - public/global data (`data.*`),
- `registry`:
  - `registry.scenarios[scenario_id]` — scenario registry,
  - `registry.merged` — effective registry (scenario + skills).

---

## 3. Core components

### 3.1. ScenarioRepository & ScenarioRegistry

- **ScenarioRepository**:
  - operates on scenarios in the monorepo (`scenarios/<id>`),
  - handles `install`, `pull`, sparse-checkout, etc.

- **ScenarioRegistry (Sqlite)**:
  - stores installed scenarios,
  - pins to version and tracks basic metadata.

These components stay close to the current implementation, with minor refactoring if needed.

---

### 3.2. ScenarioManager (lifecycle)

`ScenarioManager` is responsible for **scenario lifecycle**:

- install / uninstall / sync / push / publish,
- ensure scenario presence both in:
  - registry (Sqlite),
  - workspace monorepo (filesystem/git),
- manage capacities for node/subnet (scenario availability).

**Additional responsibilities (target state)**:

- `install_with_deps`:
  - install scenario,
  - read `scenario.yaml.depends`,
  - install and activate required skills via `SkillManager`,
- `sync_to_yjs(scenario_id, webspace_id)`:
  - read `scenario.json` (`read_content`),
  - project relevant sections into YDoc:
    - `ui.scenarios[scenario_id].application`,
    - `data.scenarios[scenario_id].catalog`,
    - `registry.scenarios[scenario_id]`,
    - top-level `data.*` overrides from `data` section,
  - emit `scenarios.synced` with `scenario_id` and `webspace_id`.

This method becomes a **generic projection entry point** used by Webspace Scenario Runtime.

---

### 3.3. ScenarioControlService

`ScenarioControlService` provides control operations:

- enable/disable scenario:
  - `scenarios/<id>/enabled` in kv  
    (missing key is treated as "enabled" so new scenarios are usable by default),
- set scenario bindings:
  - `scenarios/<id>/bindings/<key>` → value in kv.

In addition to simple writes, the service exposes helpers to:

- check if a scenario is enabled (with sensible defaults),
- read a single binding,
- list all bindings for a scenario (for inspection and tooling).

In target state bindings are used to:

- map active scenario to:
  - specific webspace,
  - user profile,
  - external channels (Telegram / browser IO),
- support scenario selection and switching without touching code.

---

### 3.4. ScenarioRuntime (procedural engine)

`ScenarioRuntime` is the **procedural engine**:

- loads `scenario.yaml`,
- executes `steps` via `ActionRegistry`,
- supports:
  - variable bag (`vars`),
  - string interpolation and placeholders (`${vars.foo}`, `${steps.step1.result}`),
  - conditions on steps (`when`),
  - logging and validation.

In target state it is used for:

- voice and console flows,
- background orchestration flows,
- admin/service routines.

UI and Yjs updates are **not** performed directly by procedural steps; instead, steps call skills or core tools which then use projection layer.

---

### 3.5. Webspace Scenario Runtime (new core service)

Webspace Scenario Runtime is a new core-level runtime (conceptual name), responsible for the interaction between:

- scenarios,
- active skills,
- webspaces,
- Yjs / web UI.

Responsibilities:

1. **Handling `scenarios.synced`**
   - for a given `(scenario_id, webspace_id)`:
     - update internal representation of effective UI for this webspace,
     - update effective catalog and registry,
     - project changes into YDoc (`ui`, `data`, `registry`),
   - keep `ui.current_scenario` consistent.

2. **Handling `skills.activated` / `skills.rolledback`**
   - maintain a per-webspace set of active skill declarations (`webui.json`),
   - merge:
     - scenario apps/widgets with skill apps/widgets,
     - scenario registry with skill registry,
     - scenario modals with skill modals,
   - apply auto-install rules from skill contributions,
   - project the merged result back into YDoc.

3. **Managing webspaces**
   - create/rename/delete webspaces,
   - seed new webspaces from a default or specified scenario,
   - maintain `data.webspaces` listing across all spaces,
   - reload/reset webspaces from scenario (debugging / recovery).

All logic that is currently implemented inside `web_desktop_skill` around:

- `_ACTIVE`,
- `_rebuild_async`,
- `desktop.webspace.*` events,
- `scenarios.synced`, `skills.activated`, `skills.rolledback`,

moves into this core runtime.

In the current codebase an initial skeleton is provided by
`WebspaceScenarioRuntime` and `WebUIRegistryEntry`
(`src/adaos/services/scenario/webspace_runtime.py`). At this stage it
focuses on computing scenario-only registry snapshots from:

- `ui.current_scenario`,
- `data.scenarios[scenario_id].catalog`,
- `registry.scenarios[scenario_id]`,
- `data.installed`.

Skill contributions are still merged by `web_desktop_skill`; future
iterations will move that responsibility into `WebspaceScenarioRuntime`.

---

### 3.6. WebUIRegistry (conceptual)

`WebUIRegistry` is a logical registry (in memory / kv / DB) that stores the **effective UI model** per webspace:

- scenario-derived application fragment (`ui.application`),
- skill-contributed apps/widgets,
- merged registry (scenario + skills),
- installed items (user choices + autoInstall).

Webspace Scenario Runtime:

- computes `WebUIRegistry` entries,
- then pushes them into YDoc so that web clients can render:

- `ui.application` — final layout and modals,
- `data.catalog` — final app/widget catalog,
- `data.installed` — installed items.

---

### 3.7. ProjectionRegistry (conceptual)

`ProjectionRegistry` defines how `ctx` data is projected into storage.

In the codebase it is represented by :class:`ProjectionRegistry`
(`src/adaos/services/scenario/projection_registry.py`), which maps
logical **context slots** to physical targets.

It maps `(scope, slot)` pairs to targets, for example:

- `(scope=subnet, slot="weather.snapshot")`
  → `y:data/skills/weather/global/snapshot` in one or more webspaces,

- `(scope=current_user, slot="profile.settings")`
  → `y:data/skills/profile/<user_id>/settings`,
  → SQL table `user_settings`.

When a skill calls:

```python
ctx.subnet.set("weather.snapshot", payload)
```

the core uses `ProjectionRegistry` to resolve and apply projections:

- compute **target set**:

  - which webspaces,
  - which YDoc paths,
  - which DB entries,
- perform **targeted writes** (no brute force over all webspaces).

Skills are not aware of these mappings.

---

## 4. Event flows

### 4.1. Scenario install and sync

1. CLI / LLM / DevOps calls:

   - `scenarios.install_with_deps("web_desktop")`.

2. `ScenarioManager`:

   - registers scenario in registry,
   - installs scenario from monorepo,
   - installs and activates dependent skills via `SkillManager`,
   - projects scenario into Yjs for the target webspace:

     - `sync_to_yjs("web_desktop", webspace_id)`.

3. `sync_to_yjs`:

   - writes scenario UI/data/registry sections to YDoc,
   - emits `scenarios.synced` (scenario_id, webspace_id).

4. Webspace Scenario Runtime handling `scenarios.synced`:

   - recomputes effective UI for the webspace,
   - updates `WebUIRegistry` and YDoc (`ui.application`, `data.catalog`, `registry.merged`, etc.),
   - ensures `ui.current_scenario` is set.

---

### 4.2. Skill activation / rollback

1. `SkillManager.activate_for_space(...)` emits `skills.activated`.

2. Webspace Scenario Runtime:

   - updates the set of active skills per webspace,
   - reloads `webui.json` for the skill (and optionally dev space),
   - recomputes:

     - merged apps/widgets,
     - merged registry,
     - auto-installed IDs,
   - updates YDoc for affected webspaces.

3. `SkillManager.rollback(...)` emits `skills.rolledback`:

   - Webspace Scenario Runtime removes skill declaration from active set,
   - recomputes and updates YDoc accordingly.

---

### 4.3. Data writes from skills via ctx

1. Skill modifies context:

   ```python
   ctx.current_user.set("todo.items", items)
   ```

2. Context layer calls Projection layer:

   - resolves `(scope=current_user, slot="todo.items")` via `ProjectionRegistry`,
   - gets a list of targets (Yjs paths, DB entries).

3. Projection layer:

   - performs targeted writes:

     - update YDoc at specified `data.*` paths (one or more webspaces),
     - update DB/kv if configured.

From the skill perspective:

- it simply works with `ctx`,
- projection is transparent and centralized.

---

## 5. Role of `web_desktop_skill` in target state

In the target architecture `web_desktop_skill` becomes a **regular skill**:

- provides:

  - one or more base modals (e.g. `apps_catalog`, `widgets_catalog`),
  - optional core widgets/apps for desktop management,
- declares its contributions and defaults in `webui.json`,
- relies on the core to:

  - maintain `_ACTIVE`-like structures,
  - rebuild catalogs,
  - manage webspaces,
  - react to scenario/skill events.

All the logic currently concentrated in `web_desktop_skill` for:

- webspace lifecycle,
- Yjs structure bootstrap,
- catalog merging,
- reaction to `scenarios.synced` and `skills.*` events,

is moved into the core runtime (`Webspace Scenario Runtime + WebUIRegistry + ProjectionRegistry`).

---

## 6. Migration notes (high level)

1. **Short term**

   - keep existing `ScenarioManager` and `ScenarioRuntime`,
   - introduce core service that mirrors current `web_desktop_skill` behaviour but lives in `services` instead of skill code,
   - route `scenarios.synced` / `skills.*` events to this service.

2. **Medium term**

   - gradually remove cross-concern logic from `web_desktop_skill`:

     - `_ACTIVE` map,
     - `_rebuild_async`,
     - webspace CRUD,
   - replace direct YDoc manipulations with calls to the new core service.

3. **Long term**

   - formalize `WebUIRegistry` and `ProjectionRegistry` as first-class services,
   - extend scenario manifest (`scenario.yaml`) with:

     - explicit `data_projections`,
     - multiple scenario types (desktop, voice_flow, service),
   - standardize how `ctx` scopes map to Yjs and storage.

This target state keeps the current behaviour as a baseline but moves responsibilities to clear, core-level components, making skills and scenarios easier to reason about, test and generate/maintain via LLM.
