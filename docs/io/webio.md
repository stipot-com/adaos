# Web IO (Webspace/Desktop)

This note explains how the hub drives the browser-based desktop shell through
Yjs, scenarios, and skills. It complements the existing IO docs by focusing on
the current "webspace" implementation that powers the Inimatic desktop used in
A2.

For the target architecture and the agreed evolutionary path, see
[Webspace Scenario Pointer/Projection Roadmap](../architecture/webspace-scenario-pointer-projection-roadmap.md).

## Architecture Overview

1. **Webspace** - each browser session attaches to a named Yjs document
   (default: `desktop`). The doc is stored in an SQLite YStore under
   `.adaos/state/ystores/<webspace>.sqlite3`.
2. **Scenario** - declarative JSON (`scenario.json`) defines desktop UI
   building blocks (apps, widgets, registry). During migration the hub may
   still seed compatibility caches in `ui.scenarios.*`, `data.scenarios.*`,
   and `registry.scenarios.*`, but active scenario switching now primarily
   updates `ui.current_scenario` and lets semantic rebuild repopulate
   effective runtime branches.
3. **Skills** - runtime packages can inject additional UI declaratives via
   `webui.json`. A core runtime (`WebspaceScenarioRuntime`) resolves
   scenario + skills into ready-to-render runtime branches such as
   `ui.application`, `data.catalog`, and `data.installed`. In this model
   `web_desktop_skill` is just another skill that contributes core desktop
   widgets and modals.
4. **Event mesh** - UI commands travel over `/ws` (`events_ws`). Each command
   becomes a domain event (`desktop.*`) with metadata containing the current
   `webspace_id`. Skills subscribe to these events and mutate the associated
   YDoc. The same control channel now also carries lightweight hub status
   pushes such as `node.status` and `core.update.status`, so browser shell
   badges can stay mostly push-driven and use HTTP polling only as a stale
   fallback when the control path goes quiet.
5. **Frontend** - the Angular/Ionic runtime consumes Yjs data through
   `YDocService`. It renders apps/widgets from the merged runtime state and
   exposes CRUD for switching or managing webspaces.

```text
Browser <--y-websocket--> Hub y_gateway <--YDoc--> SQLiteYStore
            events_ws             ^               ^
              |                   |               |
         (desktop.*)      WebspaceScenarioRuntime + skills
```

## Webspaces in Detail

* **Creation and registry** - each webspace is tracked in `y_workspaces`
  (SQLite). We store `workspace_id`, on-disk path, timestamps, and a display
  name. Bootstrap scripts install the default `web_desktop` scenario and
  `weather_skill` into the default `desktop` webspace.
* **Seeding** - `ensure_webspace_seeded_from_scenario` runs for every Yjs room
  creation and for webspace CRUD actions. It keeps migration-era compatibility
  caches available (`ui.scenarios.<id>`, `data.scenarios.<id>.catalog`,
  `registry.scenarios.<id>`) and seeds `ui.current_scenario` if needed, but
  normal scenario switch no longer depends on copying the full scenario payload
  into those branches.
* **Syncing across docs** - the core runtime updates `data.webspaces` in each
  YDoc when a webspace is created, renamed, or deleted. Each entry carries
  `{ id, title, created_at }` so any client can render the available
  desktops.
* **Switching webspaces** - the frontend remembers a preferred webspace in
  `localStorage`. Switching issues `desktop.webspace.use`, which re-seeds the
  target doc if needed, updates `/ws` routing metadata, and reconnects the
  browser to `/yws/<id>`.

## Desktop Scenario Flow

1. Scenario install calls `ScenarioManager.install_with_deps()` which:
   - installs dependent skills and activates them in the default webspace via
     `SkillManager.activate_for_space`
   - projects scenario declaratives into migration-era compatibility caches in
     the YDoc (`sync_to_yjs`)
   - emits `scenarios.synced {scenario_id, webspace_id}`
2. `WebspaceScenarioRuntime` merges:
   - scenario catalog/registry entries, marked as `source: "scenario:<id>"`
   - active skill contributions from `webui.json`, marked as
     `source: "skill:<name>"`
3. The resulting catalog is written to `data.catalog.{apps,widgets}` plus the
   filtered `data.installed`. The Angular client reads those arrays directly
   and renders icons/widgets.
4. End users toggle items via catalog modals; the UI updates
   `data.desktop.installed` optimistically and sends `desktop.toggleInstall`
   so the hub reconciles `data.installed` for everyone.

Scenario switching itself is now pointer-first by default: the hub validates
the target scenario, updates `ui.current_scenario`, schedules semantic
rebuild, and returns without eagerly copying the full payload into
`ui.application` or other effective runtime branches. Legacy branches such as
`ui.scenarios.*`, `registry.scenarios.*`, and `data.scenarios.*` remain
compatibility caches and fallback inputs during migration, not the primary
switch-time output.

## Webspace CRUD (core hooks)

The webspace lifecycle is handled by the core runtime
(`WebspaceScenarioRuntime`), not by any one skill:

| Event | Action |
|-------|--------|
| `desktop.webspace.create` | Normalize/allocate ID, insert into registry, seed from scenario, rebuild UI, sync listing. |
| `desktop.webspace.rename` | Update display name, sync listing. |
| `desktop.webspace.delete` | Remove registry entry and YStore file (except the default), sync listing. |
| `desktop.webspace.refresh` | Force re-sync of the `data.webspaces` listing across all docs. |

These events always carry `_meta.webspace_id`, so downstream helpers know the
origin but can still operate on other targets.

## Weather Skill and Webspace Routing

The weather observer publishes `weather.city_changed {webspace_id, city}`
whenever `data.weather.current.city` changes. The runtime `weather_skill`
listens for the same event type and resolves the destination webspace from
either `payload.webspace_id` or `payload._meta.webspace_id`. This ensures:

* multiple browsers in different webspaces can change the city independently
* all writes go through `async_get_ydoc(webspace_id)`, so only the intended
  YDoc is mutated

`RouterService` follows the same pattern: when emitting runtime events
destined for UI-integrated skills, include a `_meta.webspace_id` hint so
runtimes can reply into the proper doc.

For chat/TTS in webspaces prefer the dedicated "web IO" topics:

* `io.out.chat.append` -> append into `data.voice_chat.messages`
* `io.out.say` -> enqueue into `data.tts.queue`
* `io.out.media.route` -> write the normalized media route contract into
  `data.media.route`

These events are routed purely by `_meta.webspace_id`, so different
devices/webspaces can receive replies independently. `data.media.route` stays
a plain JSON subtree so browser widgets can observe one router-owned view of
need/capability/ability/attempt/degradation/observed-failure state without
depending on a specific transport adapter.

## Frontend Experience

* Toolbar displays the current webspace and offers CRUD buttons
  (create/rename/delete/refresh). Selecting a different entry sends
  `desktop.webspace.use` and reloads the UI after the hub acknowledges.
* The merged catalog already exposes DEV badges, so no additional filtering
  happens on the client.
* The weather modal directly edits `data.weather.current.city`; the hub updates
  the rest of the fields asynchronously.

## Materialization and Readiness

The Angular client treats merged runtime branches as the primary render
contract:

* `ui/application/desktop/pageSchema`
* `ui/application/modals/*`
* `data.catalog.*`
* `data.installed`
* `data.desktop`

During migration it may still fall back to legacy scenario caches such as
`ui/scenarios/<current>.application.desktop.pageSchema`,
`ui/scenarios/<current>.application.modals`, or
`ui/scenarios/<current>.pageSchema` when semantic rebuild has not hydrated the
merged branches yet.

The control API (`/api/node/yjs/webspaces/<id>`) and `YDocService` expose a
shared materialization diagnostic contract:

* `ready`
* `readiness_state`
* `missing_branches`
* `compatibility_caches`

For lighter operator polling, the same state is also split across:

* `/api/node/yjs/webspaces/<id>/rebuild`
* `/api/node/yjs/webspaces/<id>/materialization`

Both lightweight endpoints keep the expensive `runtime` snapshot optional via
`?include_runtime=1`. Normal benchmark polling should stay on the default
lightweight shape without `runtime`.

`rebuild` now also carries the last-known `materialization` snapshot when one
is available. Semantic rebuild refreshes that snapshot across its
`structure`/`interactive` phases, so hot switch loops can often observe
readiness progression from the rebuild surface alone. The dedicated
`materialization` endpoint may also return that cached snapshot while a rebuild
is still pending, or briefly after a fresh ready transition, instead of
reopening the YDoc immediately.

When those endpoints do need to inspect the document directly, they now prefer
read-only YDoc sessions so diagnostics/catalog reads avoid unnecessary flush
and room rebroadcast work on exit.

Rebuild/control surfaces also expose phase-oriented timing and apply detail so
pointer-only scenario switch can be evaluated against real runtime slices:

* `phase_timings_ms.time_to_first_structure`
* `phase_timings_ms.time_to_interactive_focus`
* `phase_timings_ms.time_to_full_hydration`
* `apply_summary.phases.structure`
* `apply_summary.phases.interactive`
* `apply_summary.fingerprint_unchanged_branches`

For repeated operator measurements, use:

* `adaos node yjs benchmark-scenario --webspace <id> --scenario-id <target> --baseline-scenario <baseline>`
* `adaos node yjs benchmark-scenario --webspace <id> --scenario-id <target> --detail`
* `adaos node yjs materialization --webspace <id>`

The command runs measured target switches, restores the baseline scenario
between iterations, can wait for terminal background rebuild completion, and
prints aggregated phase timing stats so heavy scenarios such as `infrascope`
can be compared across optimization slices. The benchmark path now polls the
lightweight `rebuild` and `materialization` control surfaces instead of
re-fetching the full webspace snapshot on every readiness loop, which keeps
operator measurements usable even when the runtime is under pressure. `--detail`
additionally prints switch/rebuild/semantic timing breakdowns plus observed
materialization milestones (`time_to_first_paint`, `time_to_interactive`,
`time_to_ready`) for each run and in the aggregate. It now also prints
`ydoc_timings_ms`, which separates YDoc/YStore session overhead from the
inner semantic rebuild timings.

When `rebuild.materialization` is present, benchmark polling now reuses that
embedded snapshot before falling back to the separate `materialization`
endpoint, which further reduces polling pressure on the hub during repeated
scenario switch measurements.

Pointer-first scenario switch now also prefers a live-room fast path when the
target webspace is already attached in memory: `ui.current_scenario` is
updated on the active room first, then persisted stale-safely in the
background. This reduces hot-path store reopen cost and improves time to first
visible switch reaction for attached clients.

If the requested local control endpoint becomes stale during supervisor
prewarm/candidate activity, the benchmark CLI can now consult supervisor
public status and retry against the currently active local runtime URL before
failing the measurement.

`readiness_state` follows a coarse ladder:

* `pending_structure`
* `first_paint`
* `interactive`
* `hydrating`
* `ready`
* `degraded`

The important distinction is that `hydrating` is an expected staged state,
while `degraded` means the runtime could not account for required desktop
branches and the client should surface a stronger recovery hint.

`compatibility_caches` exposes the migration-era legacy cache surface for
operators:

* `client_fallback_readable` reports whether current frontend fallback still
  has `ui.scenarios/<current>.application`
* `present_count` / `required_count` and `missing_branches` show how much of
  the active scenario's legacy cache surface still exists
* `switch_writes_enabled` reports whether switch-time compatibility cache
  materialization is still enabled via
  `ADAOS_WEBSPACE_SWITCH_COMPAT_CACHE_WRITES`
* `legacy_fallback_active` reports whether semantic rebuild had to read legacy
  scenario branches instead of canonical loader-backed content
* `runtime_removal_ready` becomes `true` only when effective runtime branches
  are ready, switch-time compat writes are disabled, and resolver fallback is
  no longer using legacy caches

## `webui.json` schema

AdaOS includes a JSON Schema for `webui.json` so that:

* `adaos ... skill validate` can catch structural mistakes early
* IDEs and LLM programmers can follow a stable contract when generating UI
  declaratives

Schema file: `src/adaos/abi/webui.v1.schema.json`

Recommended to add this to the top of every `webui.json`:

```json
{
  "$schema": "../../../src/adaos/abi/webui.v1.schema.json"
}
```

The schema also supports coarse staged-readiness hints on page/widget/modal
and catalog surfaces via a `load` object:

```json
{
  "widgets": [
    {
      "id": "chat_widget",
      "type": "ui.chat",
      "load": {
        "structure": "visible",
        "data": "deferred",
        "focus": "off_focus",
        "offFocusReadyState": "hydrating"
      }
    }
  ]
}
```

These hints are intent-level only. They tell the backend/renderer which
surfaces are first-paint critical versus off-focus or deferred, but they do
not encode a low-level scheduler. Keep them at page/widget/modal/catalog
granularity rather than annotating every leaf control.

## Simplifications and Limitations

* Switching webspaces still reloads the page to keep the YDoc tree simple.
  Future iterations may let `YDocService` swap docs without a full refresh.
* Deleting a webspace does not forcibly disconnect existing clients yet; they
  reconnect to a fresh doc if they issue commands afterwards.
* Event/WebSocket APIs are still dev-oriented (no auth scopes, single hub).
  Production deployments should harden these surfaces.

Despite the shortcuts, this architecture already models how declarative
scenarios, runtime skills, and the browser cooperate via Yjs. Extending it to
additional scenarios or skills only requires publishing the right events with
`webspace_id` metadata and updating the target YDoc accordingly.
