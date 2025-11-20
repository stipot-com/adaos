# Web IO (Webspace/Desktop)

This note explains how the hub drives the browser-based desktop shell through Yjs, scenarios, and skills. It complements the existing IO docs by focusing on the current “webspace” implementation that powers the Inimatic desktop used in A2.

## Architecture Overview

1. **Webspace** — each browser session attaches to a named Yjs document (default: `desktop`). The doc is stored in an SQLite YStore under `.adaos/state/ystores/<webspace>.sqlite3`.
2. **Scenario** — declarative JSON (`scenario.json`) that defines desktop UI building blocks (apps, widgets, registry). ScenarioManager projects it into every webspace doc (`ui.scenarios.*`, `data.scenarios.*`, `registry.scenarios.*`).
3. **Skills** — runtime packages that can inject additional UI declaratives via `webui.json`. `web_desktop_skill` merges scenario + skills into a ready-to-render catalog and writes it into `data.catalog` and `data.installed`.
4. **Event mesh** — UI commands travel over `/ws` (`events_ws`). Each command becomes a domain event (`desktop.*`) with metadata containing the current `webspace_id`. Skills subscribe to these events and mutate the associated YDoc.
5. **Frontend** — Angular/Ionic runtime consumes Yjs data through `YDocService`. It renders apps/widgets from the already-merged catalog and exposes CRUD for switching or managing webspaces.

```
Browser <--y-websocket--> Hub y_gateway <--YDoc--> SQLiteYStore
            events_ws            ↑              ↑
              │                  │              │
         (desktop.*)          web_desktop_skill + weather_skill, etc.
```

## Webspaces in Detail

* **Creation & Registry** — each webspace is tracked in `y_workspaces` (SQLite). We store `workspace_id`, on-disk path, timestamps, and a display name. Scripts (`tools/bootstrap*`) install the default `web_desktop` scenario + `weather_skill` into the default `desktop` webspace.
* **Seeding** — `ensure_webspace_seeded_from_scenario` runs for every Yjs room creation and for CLI/skill-triggered CRUD actions. It loads the scenario JSON, writes `ui.scenarios.<id>`, `data.scenarios.<id>.catalog`, `registry.scenarios.<id>`, and, if needed, `ui.current_scenario`.
* **Syncing across docs** — `web_desktop_skill` refreshes `data.webspaces` in *every* YDoc whenever a workspace is added/renamed/deleted. Each entry carries `{ id, title, created_at }`, so any client can present the full list.
* **Switching** — the frontend remembers a preferred webspace in `localStorage`. Switching issues `desktop.webspace.use`, which re-seeds the target doc (if missing), updates `/ws` routing metadata, and forces the browser to reconnect to the new `/yws/<id>` room.

## Desktop Scenario Flow

1. Scenario install calls `ScenarioManager.install_with_deps()` which:
   - Installs dependent skills and activates them in the default webspace via `SkillManager.activate_for_space`.
   - Projects the scenario declaratives into the YDoc (`scenarios.synced`).
   - Emits `scenarios.synced {scenario_id, webspace_id}` events that `web_desktop_skill` listens to.
2. `web_desktop_skill` merges:
   - Scenario catalog/registry entries, marked as `source: "scenario:<id>"`.
   - Active skill contributions, marked as `source: "skill:<name>"` and `dev: true` for `space="dev"`.
3. Resulting catalog is written to `data.catalog.{apps,widgets}` plus filtered `data.installed`. The Angular client simply reads those arrays and renders icons/widgets. DEV contributions are highlighted via badges.
4. End users toggle items via catalog modals; the UI updates `data.desktop.installed` optimistically and sends `desktop.toggleInstall` so the hub reconciles `data.installed` for everyone.

## Webspace CRUD Skill Hooks

`web_desktop_skill` now handles dedicated topics:

| Event | Action |
|-------|--------|
| `desktop.webspace.create` | Normalize/allocate ID, insert into registry, seed from scenario, rebuild catalog, sync listing. |
| `desktop.webspace.rename` | Update display name, sync listing. |
| `desktop.webspace.delete` | Remove registry entry + YStore file (except the default), drop cached skill UI, sync listing. |
| `desktop.webspace.refresh` | Force re-sync of the `data.webspaces` listing across all docs. |

These events always carry `_meta.webspace_id`, so downstream helpers know the origin but can still operate on other targets (e.g., create a new webspace while staying in the current one).

## Weather Skill & Webspace Routing

The weather observer publishes `weather.city_changed {webspace_id, city}` whenever the YDoc’s `data.weather.current.city` changes. The runtime `weather_skill` listens for the same event type and now resolves the destination webspace by looking at either `payload.webspace_id` or `payload._meta.webspace_id`. This ensures:

* Multiple browsers in different webspaces can change the city independently.
* All writes go through `async_get_ydoc(webspace_id)`, so only the intended YDoc is mutated.

RouterService (the text/voice router) follows the same pattern: when emitting runtime events destined for UI-integrated skills, include a `_meta.webspace_id` hint so runtimes can reply into the proper doc.

## Frontend Experience

* Toolbar displays the current webspace and offers CRUD buttons (create/rename/delete/refresh). Selecting a different entry sends `desktop.webspace.use` and reloads the UI after the hub acknowledges.
* The merged catalog already exposes DEV badges, so no additional filtering happens on the client.
* Weather modal directly edits `data.weather.current.city`; the hub updates the rest of the fields asynchronously.

## Simplifications & Limitations

* Switching webspaces currently reloads the page to keep the Y.Doc tree simple. In future iterations the YDocService can swap docs without a refresh.
* Deleting a webspace does not forcibly disconnect existing clients yet; they will reconnect to a fresh doc if they issue commands afterwards.
* Event/WebSocket APIs are still dev-oriented (no auth scopes, single hub). Production deployments should harden these surfaces.

Despite the shortcuts, this architecture already models how declarative scenarios, runtime skills, and the browser cooperate via Yjs. Extending it to additional scenarios or skills only requires publishing the right events with `webspace_id` metadata and updating the target YDoc accordingly.
