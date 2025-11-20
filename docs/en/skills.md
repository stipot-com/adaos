# skill lifecycle in adaos

per-skill runtime environment, enriched (resolved) manifests, self-tests, and A/B activation. this is a **breaking** update (no backward compatibility required). avoid hard-coding repo specifics; design for the current structure but keep the spec unambiguous. use **.adaos/skills/weather_skill/** as the reference skill for debugging and examples.

---

## objectives

* introduce a **skill runtime environment** with versioned A/B slots.
* generate and persist a **resolved.manifest.json** at install time.
* support **install → enrich → test → activate** lifecycle with rollback.
* expose minimal **cli/api** for operators and scenarios.
* codify **secrets, policies, and data layout**.
* provide **developer ergonomics** (scaffolds, lints, docs).

---

## scope checklist (deliverables & acceptance)

### 1) runtime environment layout

* [ ] design and implement per-skill runtime directory structure:

  * `skills/<name>/` (source, read-only at runtime)
  * `skills/.runtime/<name>/<version>/slots/{A,B}/` (active artifacts)
  * `skills/.runtime/<name>/<version>/active -> {A|B}` (symlink/marker)
  * `skills/.runtime/<name>/data/{db,files}/` (durable data across versions)
  * `slots/*/{env,venv|node_modules,bin,cache,logs,tmp,resolved.manifest.json}`
* acceptance:

  * creating a new version yields both slots present (empty or lazily created) and `active` points to the chosen slot.
  * paths work on linux/windows; no hard-coded absolute paths.

### 2) manifest enrichment (compiler)

* [ ] implement an **enricher** that reads `manifest.json` and writes `resolved.manifest.json` into the target slot:

  * merge platform defaults (timeouts, retries, telemetry, sandbox limits).
  * resolve `runtime` + `entry` into executable shims under `slots/*/bin/`.
  * expand permissions intents into concrete sandbox policy placeholders (no secrets written).
  * keep event schemas if provided; skip otherwise.
* acceptance:

  * `resolved.manifest.json` is self-contained for execution (tool entrypoints and interpreter paths are concrete).
  * secrets remain placeholders (`${secret:NAME}`), never materialized at rest.

### 3) install pipeline (install → build → enrich)

* [ ] implement `skill install` flow:

  * fetch code (supports monorepo sparse/forge without assuming exact layout).
  * build isolated environment (python/node/etc., adapter-driven).
  * enrich manifest into `resolved.manifest.json`.
  * target a non-active slot (default: **B** if A active, else A).
* acceptance:

  * idempotent re-install of same version does not break existing active slot.
  * partial failures clean up the target slot (no dirty state).

### 4) self-tests & hooks

* [ ] support optional `runtime/` in the skill repo:

  * `runtime/tests/` (smoke + contract; adapter-agnostic command discovery)
  * `runtime/migrate/` (data migrations)
  * `runtime/seed/` (demo/test data only)
  * `runtime/hooks/` with `pre_install`, `post_install`, `pre_activate`, `post_activate`, `pre_uninstall`
* [ ] implement test runner:

  * `smoke`: import/start within ≤10s
  * `contract`: call declared `tools[*]` with minimal fixtures and validate schemas
  * `e2e-dryrun` optional
* acceptance:

  * on test failure, installation aborts; active slot remains unchanged.
  * hooks execute with a bounded timeout and logged stdout/stderr.

### 5) activation & rollback (A/B)

* [ ] implement atomic switch of `active` to the prepared slot.
* [ ] persist previous slot for quick rollback.
* acceptance:

  * `activate` updates the marker atomically.
  * `rollback` switches back to the previous healthy slot in O(1).

### 6) cli/api surface

* [ ] provide/extend commands:

  * `adaos skill install <name>[@version] [--test] [--slot=A|B]`
  * `adaos skill activate <name> [--slot=A|B]`
  * `adaos skill rollback <name>` (to previous healthy slot)
  * `adaos skill run <name> <tool> --json '<payload>' [--timeout=..]`
  * `adaos skill status <name>` (version, active slot, health, last tests)
  * `adaos skill uninstall <name> [--purge-data]`
  * `adaos skill gc` (remove inactive slots/caches by policy)
  * `adaos skill doctor <name>` (env sanity: paths, perms, interpreter)
* acceptance:

  * all commands return non-zero on failure; machine-readable `--json` output available for status.

### 7) health & telemetry (platform-managed)

* [ ] default health/telemetry wired by the platform; skill may provide overrides:

  * detect `runtime/health.*` and prefer it if present.
  * logs default to stdout; metrics/trace controlled by node policy.
* acceptance:

  * `status` reports liveness/readiness using platform probe or skill override.
  * no telemetry endpoints are required from the skill by default.

### 8) secrets & policy resolution

* [ ] implement secret injection at **process start** only (env/fd), never persist to disk.
* [ ] apply node/tenant policy to expand intents (e.g., `network` → allowed hosts) without modifying source manifest.
* acceptance:

  * attempting to read secrets from disk yields nothing.
  * policy overrides are visible in `resolved.manifest.json` (but not secret values).

### 9) scenario integration

* [ ] scenario engine invokes tools via `call: <skill>.<tool>`:

  * lookup active slot, read `resolved.manifest.json`, execute shim.
  * scenario-level overrides for timeout/retries respected.
* acceptance:

  * calling a tool without the skill installed fails with a clear error.
  * per-call timeout is enforced even if tool blocks.

### 10) weather_skill update for debugging

* [ ] refit `.adaos/skills/weather_skill/` to the new runtime model:

  * add minimal `runtime/tests/smoke` and a tiny `contract` test.
  * optional `runtime/health.*` (fast path).
  * demonstrate `resolved.manifest.json` with a single tool (e.g., `get_weather`).
* acceptance:

  * `adaos skill install weather_skill --test` passes end-to-end on a clean workspace.
  * `adaos skill run weather_skill get_weather --json '{...}'` returns valid output.

### 11) compatibility & migration policy

* [ ] define migration path from legacy installs:

  * legacy commands print a deprecation error with a pointer to new flow.
  * provide a one-shot `migrate` command that re-installs skills into slots.
* acceptance:

  * running legacy paths does not mutate new runtime directories.

### 12) observability & logs

* [ ] standardize log locations: `slots/*/logs/` (install/test/activate/run).
* [ ] error messages are concise, actionable, and include a stable error code.
* acceptance:

  * `status --json` includes last test summary and current health.
  * failures surface the path to detailed logs.

## 13) configuration & defaults

* [ ] centralize node/tenant defaults (timeouts, retries, tracing, sandbox paths) in a single config file.
* [ ] document override order: scenario call > node policy > platform default.
* acceptance:

  * changing policy config affects **new installs/runs** but does not rewrite existing `resolved.manifest.json` retroactively (explicit design decision).

### 14) developer ergonomics

* [ ] `adaos skill scaffold <name>` generates minimal skill with:

  * `manifest.json` (name/version/tools stub)
  * optional `runtime/tests/smoke`
* [ ] `adaos skill lint <path>` validates schema and common pitfalls (no secrets, valid names).
* acceptance:

  * scaffold passes lint and install--test out of the box.

### 15) documentation

* [ ] write concise docs:

  * runtime layout and lifecycle diagram
  * resolved.manifest fields (what is compiled vs declared)
  * cli reference with examples
  * secrets & policy model
  * weather_skill walkthrough
