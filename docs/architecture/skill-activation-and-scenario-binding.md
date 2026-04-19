# Skill Activation And Scenario Binding

## Goal

Introduce one explicit runtime model for:

- always-on service skills
- lazy scenario-support skills
- on-demand tool or UI helper skills
- scenario to skill bindings

The model must improve startup behavior, reduce accidental background work, and avoid metadata drift between `skill.yaml` and `scenario.yaml`.

## Problem

Today AdaOS already has:

- skill install and activation mechanics
- scenario manifests with legacy `depends`
- runtime code that can eagerly subscribe and refresh during startup

But it does not yet have one typed architectural layer that answers:

- when should a skill be active
- who declares that a scenario needs a skill
- whether a skill is always-on, lazy, or fully on-demand
- how heavy background workers should behave when no client is using the scenario

This gap is visible in `infrascope_skill`, where a UI-facing scenario support skill can do heavy refresh work during runtime startup even when the scenario is not actively open.

## Design Principles

### Separate loaded from active

A skill may be:

- `loaded`
  - manifest is known
  - handlers can be discovered
  - lightweight event subscriptions may exist
- `active`
  - background workers may run
  - periodic refresh is allowed
  - scenario-facing projections may be eagerly maintained

This separation lets AdaOS keep discovery simple without forcing every skill into eager runtime work.

### Prefer cheap inactive handlers over deferred subscription wiring

For the first general activation architecture, AdaOS should keep lazy skills subscribed early but make inactive handlers cheap.

That means:

- subscriptions may still be registered during startup
- inactive handlers must avoid repository, git, config, filesystem, or YDoc-heavy work
- inactive handlers may enqueue lightweight invalidation or no-op quickly
- expensive refresh and background maintenance must wait for activation

This is preferred over dynamic subscribe and unsubscribe wiring because it is simpler, safer, and easier to roll out incrementally across existing skills.

### One source of truth for dependency ownership

Scenario dependency ownership belongs to the scenario manifest.

That means:

- scenario manifests declare which skills they require or optionally use
- skill manifests declare activation policy and runtime constraints
- skill manifests do not own scenario dependency truth

This avoids a fragile fully mirrored declaration model.

### Activation policy belongs to the skill

A skill still needs to declare how it behaves at runtime.

That belongs in `skill.yaml`, because the skill owns:

- whether it is `eager`, `lazy`, or `on_demand`
- whether startup activation is allowed
- whether background refresh is permitted
- whether it only becomes active when specific scenarios are active

### Legacy compatibility first

Existing scenario manifests use `depends`.

The target architecture keeps them working by treating:

- `depends` as a legacy alias of `runtime.skills.required`

New code should prefer the typed `runtime.skills` block.

## Manifest Model

### `scenario.yaml`

Target typed form:

```yaml
runtime:
  skills:
    required:
      - infrascope_skill
    optional:
      - telemetry_skill
```

Compatibility form:

```yaml
depends:
  - infrascope_skill
```

Rules:

- `runtime.skills.required` is authoritative for mandatory scenario support
- `runtime.skills.optional` means the scenario can use the skill when available, but installation or activation must not fail if it is absent
- `depends` is read as additional required skills for compatibility

### `skill.yaml`

Target typed form:

```yaml
runtime:
  python: "3.11"
  activation:
    mode: lazy
    startup_allowed: false
    background_refresh: false
    when:
      scenarios_active:
        - infrascope
      client_presence: true
      webspace_scope: active
```

Supported activation modes:

- `eager`
  - runtime should keep the skill active by default
  - suitable for service or platform skills
- `lazy`
  - skill may be loaded, but expensive work starts only after activation conditions are met
  - suitable for scenario support skills
- `on_demand`
  - no background work by default
  - suitable for tool providers and detail inspectors

Supported `when` conditions in the first typed pass:

- `scenarios_active`
- `client_presence`
- `webspace_scope`
- `webspaces`

## Runtime Interpretation

### Service skills

Service skills should usually use:

```yaml
runtime:
  activation:
    mode: eager
```

They may still choose `lazy` if the service should stay dormant until a client or route explicitly needs it.

### Scenario support skills

Scenario support skills should usually use:

```yaml
runtime:
  activation:
    mode: lazy
    startup_allowed: false
    background_refresh: false
```

They should become active only when:

- a dependent scenario is active in a webspace
- the webspace currently has an interested client, or the runtime has another explicit reason to keep projections warm

### On-demand skills

On-demand skills should usually use:

```yaml
runtime:
  activation:
    mode: on_demand
```

They should answer explicit tool, API, or UI requests, but should not keep heavy background workers alive.

## Startup Policy

User-facing lazy skills must not block runtime startup.

Target rule:

- boot-critical runtime code may load the manifest
- boot-critical runtime code must not start expensive refresh loops for a lazy or on-demand skill unless startup explicitly admits it

For a skill like `infrascope_skill`, this means:

- no eager full snapshot rebuild on `sys.ready`
- no heavy inventory refresh on the event loop during boot
- expensive refresh should move into `asyncio.to_thread(...)` or another non-loop worker path

## Webspace Semantics

Activation decisions should be made per webspace, not only globally.

Important examples:

- scenario `infrascope` is active in webspace `default`
- the same skill may remain inactive for another webspace
- background refresh should follow the active webspace scope, not all webspaces indiscriminately

`webspace_scope` is therefore part of the activation policy.

## Registry Surface

Workspace registry entries should surface normalized metadata so higher-level services do not need to parse raw manifests repeatedly.

Target normalized fields:

- skill registry entry:
  - `activation`
- scenario registry entry:
  - `skills.required`
  - `skills.optional`

This gives one cheap lookup surface for orchestration, diagnostics, and UI tooling.

## Initial Implementation Scope

The first implementation pass should do only the minimum needed to establish the architecture safely:

1. Add typed manifest parsing and normalization.
2. Surface normalized metadata in workspace registry entries.
3. Teach scenario dependency bootstrap to read `runtime.skills.required` with compatibility for `depends`.

Later passes should add:

1. runtime activation state tracking
2. per-webspace active/inactive transitions
3. lazy background worker admission
4. explicit client-presence gating
5. non-blocking refresh execution for heavy skills such as `infrascope_skill`

## Current Implementation Status

Implemented in the current pass:

1. typed manifest parsing for:
   - `skill.runtime.activation`
   - `scenario.runtime.skills.required`
   - `scenario.runtime.skills.optional`
2. compatibility mapping from legacy `depends` to `runtime.skills.required`
3. normalized activation and skill-binding metadata in workspace registry
4. scenario dependency bootstrap now reads normalized required skill bindings
5. `infrascope_skill` and `infrastate_skill` are marked as lazy scenario-support skills
6. heavy background refresh work for `infrascope_skill` and `infrastate_skill` no longer runs on the event loop thread
7. shutdown handling now suppresses executor-closing races for those background refresh workers
8. skill context resolution now prefers workspace registry metadata before falling back to repository `ensure()` logic

Important current limitation:

- AdaOS does not yet have a global activation service.
- Lazy skills are still loaded and subscribed at startup.
- The current implementation only makes their hot paths cheaper and prevents the known startup stalls.

This is an intentional transitional step:

- first remove event-loop blocking and startup regressions
- then centralize activation state and policy enforcement

Current subscription decision:

- eager skills may use normal always-registered handlers
- lazy and on-demand skills should keep early subscriptions but behave as `cheap while inactive`
- central runtime activation should eventually decide whether heavy work is admitted, but handler registration itself does not need to be deferred in the first production-safe rollout

## Migration Guidance

Recommended migration order:

1. Keep existing `depends` in current scenarios.
2. Add `runtime.skills.required` to the same scenarios.
3. Add `runtime.activation` to scenario-support skills.
4. Update runtime services to use normalized registry metadata.
5. Remove legacy-only assumptions after the runtime path no longer depends on `depends`.

## Remaining Work

Still required for the target architecture:

1. introduce a shared activation runtime that tracks `loaded` vs `active`
2. respect `startup_allowed`, `background_refresh`, and `client_presence` centrally
3. decide whether lazy skills should use:
   - cheap always-registered handlers while inactive
   - or truly deferred subscription wiring for a later optimization pass
4. move more hot-path metadata reads from repository/git/config access into registry or SQLite-backed fast paths
5. convert UI-heavy scenario skills to true on-demand detail loading instead of broad eager projection rebuilds

## Decision

AdaOS should use an asymmetric but explicit architecture:

- scenarios declare required and optional skills
- skills declare activation policy
- runtime decides when a skill is loaded versus active

This keeps dependency ownership stable, allows service-style skills, supports lazy UI-facing skills, and gives a clear path to removing startup stalls caused by eager scenario support work.
