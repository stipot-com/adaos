# Realtime Reliability Roadmap

## Goal

Fix AdaOS reliability from the top down:

1. message semantics
2. authority and degraded behavior
3. hub-root protocol hardening
4. transport ownership boundaries
5. hub-member transport abstraction and semantic channels
6. sync and media specialization
7. skills and scenarios lifecycle hardening

This ordering is deliberate.
The project must not start with sidecar or transport adapters as if they alone solved reliability.

## Current status: 2026-04-21

### Done

- architecture documents for channel semantics, authority, hub-root protocol, and transport ownership are in place
- runtime reliability model is represented in code and exposed through `GET /api/node/reliability`
- `adaos node reliability` surfaces readiness, degraded matrix, and channel diagnostics
- browser/page runtime now consumes read-only communication diagnostics through shared `runtime.reliability`, `runtime.supervisor`, and `runtime.phase0.communication` transforms instead of keeping sidecar/supervisor visibility inside one header-only component path
- node API, CLI, canonical control-plane reliability projection, and browser/page runtime now share one explicit `event_model_phase0_communication` checkpoint for the two still-open Event Model Phase 0 communication tasks
- those same reliability surfaces now also share a bounded `supervisor_runtime` snapshot, so browser-safe transition mode, candidate runtime visibility, and warm-switch evidence are carried through one canonical runtime payload instead of being reconstructed separately per surface
- those same reliability/checkpoint surfaces now also carry routed-browser active-runtime selection for root-routed `/ws`, so supervisor-aware browser continuity is explicit in node API, CLI, canonical control-plane projection, and browser diagnostics instead of living only inside bootstrap route-base selection
- those same reliability/browser surfaces now also carry one explicit sidecar enablement policy (`role_default` vs explicit env override), so the current default-on rollout for hub runtimes is observable instead of inferred from transport state alone
- Infra State shows realtime summary and transport diagnostics through Yjs-backed UI
- runtime now exposes canonical channel overview entries for `hub_root`, `hub_root_browser`, and `browser_hub_sync`
- runtime now exposes `hub_root_transport_strategy` with current transport, candidate list, recent attempts, reconnect/failure history, and active hypothesis parameters
- CLI and Infra State now surface the current hub-root transport strategy instead of only the last readiness bit
- hub runtimes now default to `hub_root` sidecar transport unless explicitly opted out, so sidecar adoption is the normal path for current transport scope rather than an opt-in experiment
- detailed channel trace is no longer a default console behavior; summary/incident output remains visible while deep console trace is explicit opt-in
- channel stability is now assessed from incidents and transport churn, not only from the last connected snapshot
- Yjs runtime diagnostics now expose explicit ownership boundaries for `ui.current_scenario`, effective `ui/data/registry` branches, compatibility caches, and `yws` transport/session lifecycle
- repo workspace fallback exists for built-in skills, scenarios, and `webui.json`
- built-in fallback for `web_desktop` restores the return path from scenario views when scenario assets are missing on a hub
- canonical runtime store for skill-local env and memory moved to `.runtime/<skill>/data/db/skill_env.json`

### In progress

- hub-root delivery guarantees are explicit for the current `hub_root.*` flow inventory, but the broader communication track remains open because sidecar ownership expansion and browser-safe continuity hardening are still incomplete
- route and root-control incident classes still need clearer separation
- transport strategy is now visible, but automatic policy-driven transport switching is not yet the default runtime behavior
- sidecar owns the current `hub_root` transport boundary and is supervisor-managed in managed deployments, but `/ws`, `/yws`, Yjs, and media transport are still outside sidecar scope
- runtime diagnostics now explicitly report that `/ws` and `/yws` remain runtime-owned with `planned_owner=sidecar`; `adaos-realtime` now also boots dedicated local proxy listeners for both paths and surfaces them as `proxy_ready`, but browser/root ingress still is not cut over to those listeners
- media/runtime diagnostics now also expose a planned continuity contract for live member media: member update should defer, while future hub restart behavior is expected to preserve an independent sidecar path
- supervisor now enforces the first conservative continuity gate on top of that model: live-media-sensitive update transitions are deferred and unsafe manual runtime restart is refused until sidecar continuity becomes a real capability instead of only a declared target
- local process/update supervision now has a separate supervisor authority in managed deployments, and default plus root-routed browser surfaces now read one shared supervisor transition/routed-base story, but warm-switch recovery soak, cleanup, and constrained-topology hardening are still in progress
- router-side media route administration now has a normalized contract in code and a browser-visible Yjs carrier at `data.media.route`, but direct `browser <-> member` admission and signaling are still not implemented
- full sidecar-owned Yjs room/session runtime is intentionally deferred as a separate redesign block; the current roadmap keeps focus on `"/yws"` transport cutover plus preparatory decoupling from runtime-local live-room ownership

### Event Model dependency note

For [Operational Event Model Roadmap](operational-event-model-roadmap.md)
Phase 0 dependency tracking, the current implementation should be read as:

- `browser/member semantic channels`: materially ready for current scope
- `Yjs ownership boundaries`: now explicit in runtime diagnostics for selector, effective branches, compatibility caches, and transport/session lifecycle
- `Yjs as SyncChannel`: complete for the current sync-channel scope; remaining browser-facing work now sits in `/yws` transport ownership migration rather than in the sync contract itself
- `sidecar-owned Yjs session runtime`: explicitly deferred beyond current Event Model `Phase 0`; for the current track it is preparatory work plus `"/yws"` transport cutover, not full room/session migration
- `hub_root` Class A coverage: explicit in runtime diagnostics and now consumed by browser/page runtime communication snapshots instead of being visible only in CLI/control-plane tooling
- `event_model_phase0_communication` checkpoint: explicit across node API, CLI, canonical control-plane projection, and browser/page runtime, so Event Model Phase 0 reads the same two open communication tasks everywhere
- sidecar rollout policy: explicit across runtime diagnostics and browser/runtime summaries, so hub default-on transport adoption can be audited separately from the still-open `/ws` and `/yws` ownership debt
- `local supervisor browser-safe continuity`: default browser/runtime surfaces now read one shared `supervisor_runtime` snapshot, and routed-browser `/ws` continuity now exposes supervisor-aware active-runtime selection explicitly; the remaining work is warm-switch soak/recovery and final hardening, not visibility
- `sidecar continuity`: now only blocks Event Model Phase 0 when the current runtime/media contract actually marks it as required
- `/ws` and `/yws` ownership migration: still explicitly incomplete and runtime-owned, but the intermediate local sidecar proxy listeners are now real and visible as `proxy_ready`; the remaining gap is ingress cutover and final ownership transfer, not missing sidecar listener scaffolding

That means Realtime Reliability is already strong enough to support Event Model
baseline alignment work, but it should not yet be treated as a fully closed
communication prerequisite for Event Model Phase 0.

### Newly implemented foundation

- per-member `browser <-> member` media capability is now advertised through `capacity.io` as `io_type=webrtc_media`
- router/reliability/media runtime now resolve member-browser direct candidates from persisted subnet capacity instead of raw `connected_total`
- normalized media route contracts now preserve `preferred_member_id` even when the selected path degrades to hub loopback or relay
- live member `node.snapshot` payloads now include local capacity, so router/reliability can use a fresher fallback view before the next heartbeat lands
- router now re-evaluates tracked browser media routes on `browser.session.changed`, member snapshot/link changes, and local `capacity.changed`

### Confirmed gaps

- transport/resource isolation is still weaker than subject naming suggests
- hub-root message inventory is not yet fully classified by delivery class and idempotency policy
- route/session incidents are still underrepresented compared to root-control incidents
- update-state visibility still disappears when the main runtime is intentionally down for restart/apply/validate
- system skills and scenarios still rely on a transitional mix of `workspace`, `repo workspace`, `runtime slot`, and `built-in seed`

## Phase 0: Architecture freeze

### Status

Completed.

### Deliverables

- [Channel Semantics](channel-semantics.md)
- [Authority And Degraded Mode](authority-and-degraded-mode.md)
- [Hub-Root Protocol](hub-root-protocol.md)
- [Transport Ownership](transport-ownership.md)

### Exit criteria

- message taxonomy approved
- delivery classes approved
- readiness tree approved
- degraded matrix approved
- authority boundaries approved

## Phase 1: Observability and incident-driven readiness

### Status

Completed for observability scope.
The model is visible in diagnostics, route/session incidents are now classified separately from root-control incidents, and transport/sidecar provenance is exposed in the runtime snapshot.
The next step is protocol hardening, not more ad-hoc diagnostics.

### Focus

Make readiness and degradation visible before changing protocol ownership.

### Work items

- keep readiness tree and degraded matrix visible in node API, CLI, and Infra State
- keep channel stability derived from incidents, reconnect churn, and watchdog failures
- separate `root_control` transport assessment from route/session incidents
- expose provenance of current transport and current artifact source in diagnostics
- make remote and direct hub diagnostics consistent when browser is connected to `:8777`

### Candidate code areas

- `src/adaos/services/reliability.py`
- `src/adaos/services/bootstrap.py`
- `.adaos/workspace/skills/infrastate_skill`
- `tools/diag_nats_ws.py`
- `tools/diag_route_probe.py`

### Exit criteria

- `ready/stable` is never reported when fresh incidents prove the channel is unstable
- route-session failures and root-control failures are visible as different incident classes
- operator can tell whether a problem is transport, route, sync, or artifact-source related

## Phase 2: Hub-root protocol hardening

### Focus

Strengthen the most critical control plane first.

### Status

Completed for the current `hub_root.*` flow inventory.
Runtime now exposes explicit hub-root traffic classes with per-class pending budgets, live subscription/backpressure metrics, route runtime pressure, and integration outbox state.
Route runtime now also separates `hub_root.route.control` and `hub_root.route.frame` semantics with distinct counters and state (`active` / `pressure` / `degraded`), so operators can tell whether the route layer is failing on tunnel lifecycle or on frame delivery.
The critical control-plane state report `hub_root.control.lifecycle` is now also explicit: hub reports carry stable `stream_id/message_id/cursor`, hub persists pending ack state locally, and root rejects stale or duplicate lifecycle reports by cursor/message id.
`hub_root.control.lifecycle` now also emits a bounded heartbeat from hub runtime, and protocol assessment treats missing or aging lifecycle acks as explicit authority health signals instead of relying only on transport reconnect status.
Runtime now surfaces this as explicit `control_authority` state (`fresh` / `aging` / `stale` / `missing`), so operators can inspect control-plane freshness directly instead of parsing assessment reasons.
The first concrete Class A stream is now explicit for `hub_root.integration.github_core_update`: hub reports carry stable `stream_id/message_id/cursor`, the hub persists pending ack state locally, and root rejects stale or duplicate state reports by cursor/message id.
The selected retryable integration flow `hub_root.integration.telegram` now carries an explicit `operation_key`, and root suppresses duplicate Telegram sends inside a bounded Redis TTL window instead of relying on text-only heuristics.
The hub-side Telegram outbox is now also persisted locally, so pending `must_not_lose` Telegram operations survive a hub restart and continue draining after reconnect instead of existing only in memory.
The selected retryable integration flow `hub_root.integration.llm` now carries an explicit `request_id`, and root suppresses duplicate LLM retries by serving a bounded cached response when the same logical request is replayed with the same request fingerprint.
Root-side report provenance is now queryable for the explicit control/core-update streams, including root receive time and ack result, so operators can verify protocol state without direct Redis inspection.
Runtime now also exposes `hardening_coverage`, and for the current `hub_root.*` flow inventory the protocol layer reports complete coverage (`6/6`) across cursor/ack streams, route semantics, idempotency keys, request keys, and durable Telegram outbox handling.

### Work items

- classify current hub-root messages by taxonomy and delivery class
- isolate control, integration, route, and sync-metadata traffic by real budgets
- split queues, workers, limits, and backpressure policy, not only subject prefixes
- add explicit per-stream cursors where replay is required
- add durable outbox only for Class A and selected integration flows
- add inbox dedupe where retry or replay exists
- define command-specific idempotency rules
- define stale-authority thresholds per hub-root flow

### Candidate code areas

- `src/adaos/services/bootstrap.py`
- `src/adaos/integrations/adaos-backend/backend/app.ts`
- `src/adaos/services/reliability.py`
- `tools/diag_nats_ws.py`
- `tools/diag_route_probe.py`

### Exit criteria

- route pressure cannot starve control readiness
- reconnect restores control readiness through explicit protocol state
- critical hub-root actions are duplicate-safe
- degraded mode is driven by explicit authority and delivery rules

## Phase 3: Sidecar as transport ownership boundary

### Status

Completed for current `hub_root` transport-ownership scope.
The sidecar now exposes a protocol-facing runtime surface with explicit ownership boundary, transport readiness, control readiness, reconnect counters, quarantine/supersede history, and transport provenance.
Sidecar lifecycle is also independently observable and restartable through the local control API and CLI, and managed deployments now place that lifecycle under `adaos-supervisor` instead of the runtime lifespan.
This completion is intentionally transport-only: the sidecar owns the `hub_root` NATS transport lifecycle, but does not yet own route tunnel transport, Yjs sync transport, or media transport.
The intermediate ownership split is now explicit in diagnostics: current sidecar scope, lifecycle manager, and planned next boundaries are exposed alongside runtime-owned `/ws` and `/yws` transport ownership.

### Focus

Move transport ownership where it reduces blast radius, without moving protocol semantics.

### Work items

- define sidecar status API in protocol terms
- expose control readiness, route readiness, reconnect diagnostics, and transport provenance
- ensure hub main process remains owner of durability and degraded policy
- use sidecar first for hub-root transport lifecycle after protocol guarantees are explicit

### Candidate code areas

- realtime sidecar runtime
- hub startup and shutdown wiring
- diagnostics aggregation

### Exit criteria

- transport failures are isolated from hub business logic
- sidecar does not become a hidden protocol authority

## Phase 3.5: Local supervisor as process and update authority

### Status

In progress.

The next reliability gap after transport isolation is local process/update supervision.
AdaOS currently loses its primary local admin/update surface exactly when the runtime is stopped for update or restart.
This phase introduces a dedicated `adaos-supervisor` that remains available while the main runtime is down.
Production runtime remains slot-only; root promotion becomes a separate post-validation step for bootstrap-managed code.
Current MVP coverage now includes slot-first validation, explicit root-promotion states, an explicit `root restart in progress` attempt stage after root promotion, forced shutdown recovery for hung runtime restarts, one queued subsequent transition after an in-flight transition, minimum-interval scheduling for normal update requests, operator-driven defer for planned/countdown updates, a browser-shell transition badge, pushed browser-safe supervisor transition delivery over the control `/ws` channel with `/hubs/<id>/api/supervisor/public/update-status` fallback polling when that control path is unavailable, a canonical supervisor runtime object in the control-plane model so Infrascope/overview surfaces can project transition state as an operator runtime instead of only a transport outage, browser-safe and canonical operator surfaces that both carry the current transition `action` plus passive-candidate prewarm stage, formal safe supervisor actions in that canonical object for `cancel`, `defer`, and `promote_root` where the transition state allows them, routed root-facing subnet snapshots that retain transition action/scheduling/passive-candidate metadata for non-default browser topologies, a slot-bound runtime-port model with an explicit supervisor-side warm-switch admission decision (`warm_switch` vs `stop_and_switch`) based on reserved A/B ports and local memory headroom, per-runtime identity (`runtime_instance_id`, `transition_role`) threaded into supervisor/root-facing reports so parallel runtimes no longer collapse into one opaque `hub_id`, runtime self-identification/guardrails so candidate runtimes are skipped by local fallback control discovery and reject mutating local update commands until cutover, early inactive-slot preparation with deferred skill-runtime commit so heavy slot build work moves before shutdown without mutating live skill runtime selection during countdown, and real candidate-runtime fast cutover where supervisor promotes/adopts a prewarmed passive candidate and falls back to stop-and-switch if that authority handoff fails.
That MVP now also includes a first live-media continuity gate: supervisor consults runtime reliability before restart/update, defers transitions that would violate the declared continuity contract, and keeps that reason visible through planned update state.
The shared reliability/runtime surfaces now also carry that transition state directly through `supervisor_runtime`, and the routed-browser `/ws` path now surfaces supervisor-aware active-runtime base selection through the same checkpoint family, so default browser, routed browser, CLI, and canonical control-plane consumers no longer need separate heuristics to see transition mode, candidate runtime, or routed continuity evidence.
The remaining supervisor gap is no longer the existence or visibility of fast cutover itself but the last-mile hardening around it: smoother root/browser signaling during warm-switch authority handoff so the shell is not reduced to generic reconnect churn, plus more soak/recovery coverage for dual-runtime registration, candidate cleanup, and constrained-memory fallback.

### Focus

Separate:

- transport ownership
- runtime execution ownership
- local process/update supervision ownership

The sidecar remains transport-only.
The supervisor becomes the authority for local runtime lifecycle and update attempt state.

### Work items

- define `adaos-supervisor` local authority boundary
- persist explicit local update attempt state independent of runtime bind state
- add restart/apply/validate deadlines and stale-attempt recovery
- move update-status and restart control to a supervisor API that remains live while runtime is down
- make systemd target supervisor instead of the main runtime process
- keep production runtime sourced from slot `A|B` even after supervisor/root updates
- validate every candidate in an inactive slot before allowing any root/bootstrap promotion
- detect bootstrap-managed file changes and surface `root_promotion_required` explicitly instead of silently mixing slot and root drift
- keep supervisor-owned sidecar lifecycle observable through both supervisor and runtime-compatible node-control surfaces
- retain standalone runtime fallback only for non-supervised deployments, without turning sidecar into protocol or update authority
- migrate installed skill runtimes as an explicit core-update subflow rather than assuming old interpreter dependencies remain valid
- persist per-skill migration diagnostics (`prepare` / `test` / `activate` / `rollback` / `deactivate`) in core-update results
- surface skill migration failures and selective post-commit deactivations in Infra State and Infrascope
- keep supervisor transition state visible in canonical operator projections (`active_runtimes`, health strips, recent changes) rather than only in ad-hoc browser badges
- separate runtime liveness from listener/API readiness in supervisor-visible status
- surface the active managed runtime command/source in supervisor diagnostics
- surface active-slot structure validation in supervisor diagnostics so broken slot layouts fail explicitly
- harden diagnostic skills so Yjs-backed operator surfaces keep the last usable local snapshot during transient control-plane file failures
- keep browser-facing update visibility alive through pushed supervisor status on `/ws`, with supervisor polling only as fallback while `/ws` and `/yws` reconnect during slot restart
- expose a read-only browser-safe supervisor transition surface so restart/update state is not collapsed into generic `offline`
- distinguish browser-facing `hub restarting`, `update applying`, `rollback`, `root promotion pending`, `root restart in progress`, and `update failed` from ordinary transport reconnect state
- surface `planned`, `deferred`, minimum-window scheduling, and queued follow-up transition state through that same browser-safe/read-only supervisor surface
- extend the routed read-only supervisor transition surface across every browser deployment topology, not only the default `/hubs/<id>/api/...` entry path
- reserve stable runtime ports per slot so supervisor can reason about `active` and `candidate` runtimes explicitly
- add a memory gate that decides when dual-runtime warm-switch is safe and when supervisor must fall back to stop-and-switch
- surface `transition_mode`, candidate runtime URL/port, and warm-switch admission reason in operator and browser-safe status
- assign every runtime process a stable-per-boot `runtime_instance_id` and `transition_role` so root/NATS/browser can distinguish `active` from `candidate`
- keep candidate runtimes passive on root-routed traffic subjects until cutover so prewarm does not create duplicate hub traffic consumers
- automatically prewarm passive candidate runtime when warm-switch is admitted, surface its readiness/failure in supervisor/browser-safe status, and keep the candidate passive until supervisor explicitly commits cutover
- harden fast-cutover authority handoff so promoted candidate runtime becomes the sole live root/browser traffic owner without ambiguous overlap
- add stronger soak/recovery coverage for candidate promotion fallback, stale candidate cleanup, and low-memory warm-switch downgrade paths

### Candidate code areas

- `src/adaos/apps/autostart_runner.py`
- `src/adaos/services/core_update.py`
- `src/adaos/services/autostart.py`
- `src/adaos/apps/cli/commands/setup.py`
- `src/adaos/apps/cli/commands/node.py`
- `src/adaos/apps/supervisor.py`

### Exit criteria

- update status remains visible while runtime is stopped
- stale `restarting` / `applying` states resolve deterministically
- rollback decision is owned by supervisor logic rather than only runtime-side best effort
- sidecar remains transport-only and does not absorb process/update authority
- operators can identify which installed skill failed during a core migration and at which stage
- operators can distinguish `slot validation`, `root promotion pending`, and `root restart in progress` from supervisor-visible state
- browser header/status surfaces can distinguish controlled supervisor-managed restart/update transitions from plain hub offline or transport loss
- routed browser sessions can continue reading live supervisor transition state even while runtime `/api`, `/ws`, and `/yws` are unavailable
- browser/operator transition surfaces can also distinguish a passive `candidate` runtime from the current `active` runtime by `runtime_instance_id`, role, and candidate readiness state instead of showing only one opaque "hub restarting" bucket
- operators can tell whether the next transition is planned as `warm_switch` or `stop_and_switch`, and why
- local fallback control resolution cannot accidentally target a passive `candidate` runtime as if it were the active admin endpoint
- root/browser diagnostics can distinguish concurrent `active` and `candidate` runtimes by explicit runtime instance identity instead of only `hub_id`
- when warm-switch is admitted and candidate prewarm succeeds, supervisor can promote/adopt that candidate instead of forcing a second fresh runtime launch, while fallback to stop-and-switch remains deterministic

## Phase 4: Hub-member semantic channels

### Status

Completed for current browser/hub-member semantic ownership scope.
Runtime now exposes explicit hub-member semantic channels (`command`, `event`, `sync`, `presence`, `route`, `media`) with one selected active path per channel, live transport evidence from `/ws`, `/yws`, WebRTC datachannels, and root relay runtime, plus explicit failover order, freeze windows, and duplicate-suppression notes.
Frontend command delivery and sync-provider creation now also use a shared semantic channel selector instead of branching directly on WebRTC-vs-WS in application code, and the web header transport indicator now follows the selected semantic member path instead of raw WebRTC peer state.
Frontend transport notifications now also follow semantic member-path transitions, and the client no longer keeps a separate application-level `useWebRtc` authority for command routing.
The browser shell now consumes semantic member transport state directly from the channel selector service, while raw WebRTC visibility/reconnect state is pushed down into the transport/runtime layer instead of remaining an app-shell concern.
Yjs startup no longer performs raw WebRTC upgrade orchestration itself; it now asks the member-transport layer to prepare direct paths and then builds sync providers through the semantic selector.
The browser connection client no longer owns raw WebRTC callback wiring either; low-level RTC state is now contained inside the transport runtime and the semantic channel selector.
The browser-side semantic selector now also carries explicit live path evidence for routed `/ws` and `/yws` (`idle` / `connecting` / `connected` / `disconnected`) instead of treating those fallback paths as implicitly healthy, so UI transport state and channel snapshots reflect real browser transport state rather than static assumptions.
The frontend semantic channel model now also declares `route` and `media` explicitly, and routed fallback availability is now derived from live browser-side path evidence instead of being treated as always-available by definition.
Command-path exceptions that must stay on the control plane, such as `rtc.*` signaling, are now also resolved inside the semantic channel layer instead of being hard-coded in the browser connection client.
Control-plane subscription orchestration is now also part of that semantic layer: browser member channels track the active control subscription set, dedupe it, and replay it on control-WS reconnect instead of leaving resubscribe behavior as ad-hoc client logic.
Control-plane session bookkeeping is now also owned there: browser member channels track control-WS session state, reconnect/open counts, close reasons, in-flight command count, and last command completion outcome instead of leaving that protocol state implicit inside the client socket wrapper.
Command envelope shaping and ack parsing for the browser control path are now also routed through the semantic member-channel layer, and browser header UI can surface the semantic snapshot (`command` / `sync` / `route` / `media`, control session state, recovery state) instead of exposing only a raw transport icon.
The browser client no longer owns pending control-command lifecycle either: in-flight command registration, ack completion, timeout/error/close failure, and route-health interpretation are now semantic-layer responsibilities rather than socket-wrapper details.
Direct-path probing is now also user-honest: when relay `ws/yws` paths are healthy, semantic channel authority no longer flips to a merely `connecting` WebRTC candidate, and repeated direct-path failures move into exponential-backoff background probes instead of keeping browser status stuck in an over-optimistic `recovering` state.
Raw control-WS session creation is now also driven through the semantic member-channel layer rather than the browser client owning its own socket/promise lifecycle, and `media` is now explicitly frozen as `out_of_scope` in the semantic snapshot instead of remaining an unnamed implicit gap.
Direct-path enablement is now also decided inside the semantic member-channel layer: browser Yjs startup no longer parses `?p2p` / `?webrtc` flags itself, and the connection client no longer takes an application-owned `allowDirect` flag when preparing member transport.
Direct-path recovery policy is now also owned by the semantic member-channel layer: browser member channels decide when visibility or control-WS recovery should trigger a direct-path renegotiation, while the low-level WebRTC transport remains only the executor of that renegotiation.
Sync self-heal policy is now also part of that semantic layer: routed Yjs fallback recovery on first-sync timeout or provider disconnect is tracked and gated by browser member-channel policy, while `YDocService` remains only the executor that recreates the concrete sync provider.
Hub-member update propagation now also exists as an explicit Phase 4 concern: hub mirrors `core.update.status` to connected members over the member link, members mirror that state locally as `hub.core_update.status`, and member runtimes can follow the hub-triggered core update through their own local admin API instead of relying on out-of-band coordination.
Node naming and member observability were also moved into the same semantic layer checkpoint: `node.yaml` now carries `node.node_names`, member hello advertises those names to the hub, reliability exposes canonical `hub_member_connection_state`, and Infra State can project node selectors plus per-member connection/update visibility on top of that runtime model.
Hub/member observability now also carries compact remote runtime snapshots over the member link: members periodically publish their own local lifecycle/build/update state to the hub, the hub stores that snapshot as part of hub-member connection state, and Infra State node tabs can render selected member build/update state from remote data instead of only showing link-level telemetry.
Member rollout semantics are now modeled on top of those snapshots as well: hub-member connection state distinguishes fresh/pending/stale member snapshots, derives a cohort-level rollout state (`nominal` / `transitioning` / `pressure` / `degraded`) from member update progress, and surfaces that rollout summary in CLI and Infra State instead of treating all connected members as equally healthy.
Hub/member observability is now also on-demand instead of purely periodic: the hub can request a fresh remote member snapshot over the member link, Infra State uses that when selecting or refreshing remote member tabs, and canonical channel overview now includes `hub_member` control plus `member_hub_sync` alongside the earlier hub-root channels.
Those same hub-member semantics are now also promoted into the canonical readiness/degraded model: readiness tree exposes explicit `hub_member` and `member_sync` nodes, and degraded matrix can now explain whether remote snapshot projection or hub-triggered member rollout is currently allowed.
Hub-triggered member rollout is now also an explicit operator control surface instead of passive follow only: the hub can request `update` / `cancel` / `rollback` on a selected member over the member link, members execute that through their own local admin API, and CLI plus Infra State surface the last remote control request/result together with the member snapshot.
Hub/member observability no longer depends only on an active member link either: hub runtime now also tracks known members from subnet directory / heartbeat state, and Infra State node tabs can render those observed members even before a full snapshot-bearing member link is established.
This is still intentionally a semantic-path checkpoint, not a full transport rewrite: signaling and subscription setup remain explicit control-plane WS behavior, and transport-specific orchestration still exists around negotiation, reconnect, and low-level datachannel runtime.
The remaining direct-path reconnect policy is now also mostly lifted into the semantic layer: browser member channels decide when a disconnected direct path should first try `ICE restart` versus a full renegotiation, apply disconnect grace and exponential backoff there, and expose the low-level RTC runtime snapshot (`rtc state`, ICE state, last failure reason) to the browser UI. The WebRTC transport service now acts primarily as an executor of SDP/ICE operations instead of hiding retry policy inside the transport runtime itself.
Control-plane RTC signaling ownership is now also aligned with that boundary: browser member channels route inbound `rtc.answer` / `rtc.ice` messages and outbound local ICE candidates through the semantic layer, while the low-level WebRTC transport remains responsible only for peer lifecycle, SDP/ICE execution, and datachannel runtime.
With that boundary in place, the browser-side exit criteria are met for implementation scope: one logical stream has one active authority path, failover rules are explicit, and application/browser adapters no longer decide transport semantics themselves. Remaining work is live-session validation and later media specialization, not more structural Phase 4 refactoring.

### Focus

Build abstraction from logical channel semantics, not from transport names.

### Work items

- define `CommandChannel`, `EventChannel`, `SyncChannel`, `PresenceChannel`, `RouteChannel`, `MediaChannel`
- map existing `/ws`, `/yws`, WebRTC data channels, and root relay traffic to those channel types
- define path selection, failover, freeze period, and duplicate suppression rules
- keep one active authority path per logical stream unless multipath is explicitly designed

### Candidate code areas

- `src/adaos/services/webrtc/peer.py`
- browser/hub websocket gateways
- route proxy logic

### Exit criteria

- one logical stream has one active authority path
- failover rules are explicit
- adapters no longer leak transport semantics into application code

## Phase 5: Yjs as SyncChannel

### Status

Completed for the current sync-channel scope.
Hub and browser runtime surfaces now expose an explicit SyncChannel contract:
bounded replay window, snapshot+diff recovery, optional browser IndexedDB
persistence, explicit resync controls, and explicit separation of ephemeral
awareness from document recovery. Remaining `/yws` ownership migration belongs
to the sidecar/transport boundary work, not to the SyncChannel contract itself.

### Focus

Make Yjs transport-independent without building a second distributed system around it.

### Work items

- append-only bounded update log
- snapshot + diff recovery
- client local persistence
- awareness explicitly ephemeral
- explicit resync path after route or transport churn

### Candidate code areas

- sync engine
- Yjs gateway and recovery paths
- local persistence integration

### Exit criteria

- document updates survive reconnect within replay window
- Yjs reliability is not duplicated blindly across transport, log, and UI layers
- awareness may drop without compromising document state

### Completed for current scope

- hub-side YStore runtime now exposes bounded log and snapshot+diff state for operator diagnostics
- browser sync now has an explicit resync path and runtime snapshot instead of scattered provider recreation logic
- node reliability / hub-root status surface Yjs sync runtime alongside transport and protocol state
- browser header now exposes a manual Yjs resync action, separate from scenario reseed/reload
- browser sync runtime now separates document recovery from ephemeral awareness state
- hub/browser runtime surfaces now also expose the SyncChannel contract explicitly instead of requiring operators to infer it from scattered implementation details
- node API / CLI now expose explicit Yjs runtime and snapshot-backup control paths
- hub-side node API / CLI now expose explicit per-webspace Yjs reload/reset control paths
- Infra State now surfaces Yjs runtime state and local Yjs backup/reload/reset operator actions
- node API / CLI and Infra State can now focus Yjs diagnostics and local actions on a selected webspace instead of assuming `default`
- hub-side node API / CLI and Infra State now expose explicit per-webspace Yjs restore-from-snapshot recovery when a disk snapshot exists
- legacy `/api/yjs/reload` has been removed entirely; node-scoped per-webspace Yjs controls are the only supported override path
- Yjs sync runtime now carries an explicit operator recovery playbook (`reload` vs `restore` vs `reset`) and surfaces that policy consistently in CLI and Infra State
- Yjs runtime now computes immediate recovery guidance (`backup first` vs direct `reload`) from live webspace state and surfaces the recommended next action across CLI and Infra State
- selected webspace manifest/projection state is now surfaced alongside Yjs runtime, including home scenario, source mode, rebuild status, and `go-home` guidance when projection drifts from home
- node API / CLI and Infra State now expose `set-home-current` so operators can explicitly adopt the current projected scenario as the new webspace home without typing a scenario id
- direct node-scoped Yjs/webspace control paths now publish canonical `node.yjs.control.*` events, and Infra State refreshes from those events instead of relying only on the original desktop bus commands
- Yjs recovery/control scope is now explicit as hub-local-only in operator surfaces; remote member tabs show that sync control is not applicable there

## Phase 6: Media plane

### Status

In progress with explicit media-plane policy, bounded relay authority, relay throughput tuning, and live operator validation.
Local media upload/playback MVP exists as an intentionally isolated direct-local HTTP path, reliability exposes media runtime separately from control/sync readiness, and browser semantic channels classify file media as `direct_local_http` vs `root_routed_http_relay`.
There is also a dedicated bounded root media relay path (`/hubs/<id>/media/*`) for upload and playback, separate from the generic buffered `/hubs/<id>/api/*` route proxy, and that relay now uses larger bounded chunks plus unbuffered nginx proxying for materially better large-file throughput.
WebRTC audio/video tracks are now part of the target Phase 6 scope as a direct live-validation path: browser camera/microphone tracks are negotiated on the same peer and looped back through the hub for real end-to-end operator testing.
Phase 6 is complete for the current scope: bounded file-media authority and direct hub loopback validation are in place. This is still not a general multi-party media plane: the current A/V scope is hub loopback validation plus bounded file-media authority, not a full broadcast/session mesh.

### Focus

Keep media architecture separate, but do not let it block core messaging stabilization.

### Confirmed Phase 6 gap: peer rebuild couples media to control and sync

Current browser-hub P2P behavior still has one architectural weakness:
media lifecycle is coupled too tightly to peer lifecycle.

Today the browser transport may intentionally rebuild the whole `RTCPeerConnection`
when live media starts or stops, and the hub currently favors replacing the
existing peer on every fresh offer.
This is acceptable for operator-grade loopback validation, but it is not an
acceptable steady-state design for a reliable multi-channel media plane.

That coupling creates self-inflicted failure modes:

- starting or stopping media can tear down `events` and `yjs` data channels
- a UI component destroy path can indirectly trigger full P2P renegotiation
- direct media actions and direct recovery policy share too much blast radius
- file transfer over the media data channel can be interrupted by unrelated media actions
- media growth toward multiple concurrent sources cannot be implemented safely on top of "replace the whole peer"

The next Phase 6 target is therefore not "more loopback features first".
It is to decouple media-session behavior from peer-session behavior.

Another confirmed gap is topology:
current direct WebRTC covers `browser <-> hub`, but not a direct
`browser <-> member` media peer for member-hosted media producers.
That means a media-producing skill running on a member cannot yet expose its
best direct browser path even when the network topology would allow it.

### Target architecture

The target browser-hub direct transport model is:

- one long-lived peer session per browser member session
- one stable control/data container peer, not a disposable peer per media action
- independent logical channel lifecycles on top of that peer:
  - control/events
  - sync/Yjs
  - file media transfer
  - live media tracks
- media source enable/disable must not require tearing down data-path authority
- failures in one logical media flow must degrade locally before escalating to whole-peer recovery

In that target design, `RTCPeerConnection` is transport state, while media is
workload state carried over that transport.
The browser shell, scenario layer, and widget lifecycle must not own peer
teardown authority except for explicit session shutdown.

### Architectural invariants

- peer lifecycle and media lifecycle are separate state machines
- control and sync channels remain valid when live media is added, removed, muted, or replaced
- UI open/close behavior must not implicitly stop or rebuild the underlying peer session
- one logical stream keeps one active authority path, but different logical streams may use different paths simultaneously when explicitly designed
- direct media recovery is local-first:
  - restart ICE if the peer is intact
  - renegotiate the affected media shape if needed
  - rebuild the whole peer only as the final fallback
- peer replacement must be explicit and versioned, not an automatic side effect of every fresh offer

### Media multi-channel target

Phase 6 should evolve toward a multi-channel media model rather than one
"live session" toggle.

The intended shape is:

- one control peer session
- multiple media subflows on that session
- explicit per-flow identity such as `media_session_id`, `slot_id`, or source id
- independent enable/disable and health for:
  - microphone
  - camera
  - screen share
  - file upload / binary media transfer
  - hub loopback validation
  - later broadcast or room-style media flows

This does not mean uncontrolled multipath authority.
It means media concurrency must be explicit inside the semantic channel model
instead of being approximated by repeated whole-peer rebuilds.

### Member-browser direct media target

Phase 6 should also grow from one direct topology into an explicit set of
direct media topologies:

- `browser <-> hub`
- `browser <-> member`
- bounded relayed fallback when direct peer setup is not allowed or not possible

The intended authority split is:

- router chooses which runtime should answer the media need
- hub and/or root act as rendezvous, signaling, and policy authorities
- the direct media peer may still terminate on the selected member rather than on the hub

This is especially important for member-hosted media skills.
If a media server skill runs on a member, the preferred target state is not
"member sends media to hub and hub re-originates it by default".
The preferred target state is:

- router resolves the member as the media producer
- signaling is mediated by hub/root as needed
- browser attempts a direct peer to that member when policy and topology allow it
- fallback remains available through bounded relay paths when direct media is unavailable

### Preferred implementation shape

The preferred technical direction is:

- keep `events` and `yjs` data channels long-lived once the peer is established
- stop treating `negotiate()` as synonymous with `close existing peer and rebuild`
- move to serialized renegotiation with one negotiation authority at a time
- keep stable peer/session identifiers so stale answers or superseded negotiations can be rejected safely
- use transceiver- and sender-level media control where possible:
  - pre-created transceivers
  - `replaceTrack(...)`
  - direction changes such as `inactive`, `recvonly`, `sendrecv`
- keep media upload/data transfer logic independent from live A/V track lifecycle

### Migration roadmap

#### Stage 1: remove UI-owned peer teardown side effects

- stop binding widget/component destroy directly to live-media shutdown semantics
- ensure closing a modal or unmounting a media widget only detaches UI observers unless the user explicitly requested media stop
- document and test that ordinary scenario/UI transitions do not call whole-peer teardown implicitly

#### Stage 2: split peer shutdown from renegotiation

- separate `closePeer()` from `renegotiatePeer()` in the browser transport runtime
- remove the current "always `close()` before `negotiate()`" behavior
- preserve existing data channels and peer state when only media shape changes
- keep explicit full teardown only for logout, page unload, protocol incompatibility, or unrecoverable peer corruption

#### Stage 3: stop unconditional hub-side peer replacement

- hub should no longer replace an existing peer on every fresh browser offer by default
- introduce explicit peer/session generation or epoch checks
- only supersede the old peer when the offer belongs to a new session generation or the old peer is proven unrecoverable

#### Stage 4: introduce serialized negotiation ownership

- add a negotiation mutex / coordinator on the browser side
- coalesce multiple local changes into one negotiation pass
- implement glare-safe / stale-answer-safe handling so concurrent UI actions do not create negotiation races
- expose negotiation diagnostics separately from raw connection state

#### Stage 5: move media control to subflow semantics

- treat live media as one or more explicit media subflows on top of the stable peer
- add per-subflow health, diagnostics, and recovery tracking
- keep file upload over media data channel independent from audio/video track transitions
- make semantic channel snapshots report media-subflow readiness separately from control/sync readiness

#### Stage 6: support real media multi-channel behavior

- support multiple simultaneous local sources without peer rebuild
- support explicit policy for which media subflows are direct, relayed, loopback-only, or bounded
- keep multi-channel media observable in operator surfaces without collapsing it into a single boolean `webrtc connected`

### Exit criteria for the next Phase 6 checkpoint

- starting or stopping live media does not tear down control and sync data channels
- closing or reopening a media UI surface does not implicitly rebuild the peer
- direct file upload over media data channel survives unrelated media UI transitions
- whole-peer rebuild becomes an explicit last-resort recovery action rather than a routine media operation
- runtime and operator diagnostics distinguish:
  - peer session health
  - control/sync path health
  - per-media-subflow health
- the architecture is ready for true media multi-channel expansion without introducing hidden authority conflicts between control, sync, and media

### Implementation plan by code area

This section turns the target architecture into an implementation backlog tied
to the current codebase.

#### Browser transport runtime: `webrtc-transport.service.ts`

Current role:

- owns peer creation and teardown
- owns data-channel wiring
- owns media upload data channel
- currently equates `negotiate()` with "close old peer and build a new one"

Current coupling to remove:

- `negotiate()` begins with unconditional `close()`
- media start/stop depends on full peer rebuild
- media upload session is tied to whole-peer lifetime rather than a narrower channel/session scope

Planned refactor:

- split lifecycle entry points into explicit operations:
  - `ensurePeer(sendCommand)`
  - `renegotiatePeer(reason, options)`
  - `closePeer(reason)`
  - `restartIceTransport()`
- preserve an existing peer when renegotiation is only updating media shape
- add explicit negotiation state tracking:
  - peer session id
  - negotiation id
  - negotiation in-flight flag / mutex
  - last negotiated media shape
- add explicit media sender/transceiver registry:
  - audio sender/transceiver
  - video sender/transceiver
  - later screen-share sender/transceiver
- change live media control from "replace peer" to:
  - acquire local track
  - attach or replace track on an existing sender/transceiver
  - request serialized renegotiation only if SDP shape changed
- keep media upload over the `media` data channel independent from live track enable/disable as much as possible

Expected code changes:

- replace the current unconditional `this.close()` path in `negotiate()`
- introduce internal helpers for peer bootstrap vs peer update
- introduce a transport snapshot that distinguishes:
  - peer state
  - negotiation state
  - media-subflow state
- ensure pending media upload is failed only when the media data channel or peer actually becomes unusable, not merely because UI toggled a live preview

Minimum acceptance tests:

- direct command/events data channel stays open while starting camera loopback
- direct Yjs data channel stays open while stopping microphone loopback
- file upload can continue across unrelated media preview UI detach/reattach

#### Browser semantic orchestration: `hub-member-channels.service.ts`

Current role:

- owns semantic path selection and direct-path recovery policy
- owns initial direct negotiation and direct recovery
- currently triggers full renegotiation for media start/stop

Current coupling to remove:

- `startMediaLoopback()` calls full `rtc.negotiate(...)`
- `stopMediaLoopback()` calls full `rtc.negotiate(...)`
- direct recovery and media lifecycle both escalate too quickly to whole-peer rebuild semantics

Planned refactor:

- keep semantic ownership of policy, but narrow the commands it sends to the transport runtime
- replace "start/stop media loopback => full renegotiate" with explicit media-intent calls:
  - `ensureLiveMediaSubflow(...)`
  - `disableLiveMediaSubflow(...)`
  - `refreshMediaNegotiation(...)`
- separate recovery ladders:
  - peer recovery ladder
  - live-media recovery ladder
  - media-upload recovery ladder
- expose media-subflow evidence in the semantic snapshot, for example:
  - `live_audio`
  - `live_video`
  - `media_upload`
  - later `screen_share`
- keep control/sync path selection authority unchanged unless peer-level health actually degrades

Expected code changes:

- `prepareDirectPaths()` should create or validate a stable peer session, not create an assumption that every future media action will rebuild it
- `startMediaLoopback()` should become a policy method that requests the live media subflow rather than a whole-peer rebuild
- `stopMediaLoopback()` should disable the relevant subflow and only renegotiate the affected media shape if necessary
- direct recovery should prefer:
  - `ICE restart`
  - targeted peer renegotiation
  - full peer rebuild last

Minimum acceptance tests:

- semantic `command` and `sync` active paths remain unchanged while live media starts and stops
- `media` semantic state can degrade independently without forcing `command` and `sync` to fallback
- visibility changes or modal/widget lifecycle do not trigger whole-peer rebuild unless actual peer health requires it

#### Router authority for response and media routing

Current gap:

- media path choice and skill-response path choice are still too fragmented across local helpers
- transport layers know too much about fallback intent
- there is no single semantic owner of "need -> capability -> ability -> attempt -> degradation -> observed failure"

Target role:

- router should become the semantic administrator of response routing for skills and scenarios
- router should also become the semantic administrator of browser-visible media route choice
- transport implementations remain executors of the chosen route, not owners of route semantics

Current foundation in code:

- `resolve_media_route_intent(...)` defines one normalized route-administration contract for media needs
- reliability and media runtime snapshots already expose that contract through the shared vocabulary:
  - `need`
  - `capability`
  - `ability`
  - `attempt`
  - `degradation`
  - `observed failure`
  - `monitoring`
- `RouterService` now projects this router-owned view into `data.media.route` so browser surfaces can observe the chosen route without inferring it from transport internals
- the router now also advances an explicit media-route `attempt` contract with `sequence`, `switch_total`, `previous_route`, `previous_member_id`, `last_switch_at`, `observed_failure`, and refresh cause whenever the chosen topology or member target changes
- `browser <-> member` direct media is represented as a capability foundation only until per-member capability inventory and signaling rendezvous are implemented

Route-administration state to make explicit:

- need:
  - what the caller is trying to receive or deliver
- capability:
  - which targets advertise they can satisfy that need
- ability:
  - whether those targets are currently reachable, authorized, and healthy enough
- attempt:
  - which target/path is currently active or in-flight
- degradation:
  - which fallback class is allowed
- observed failure:
  - which concrete incident happened on the current route
- monitoring:
  - which signals determine recovery, failover, or operator-visible degraded mode

Implication for implementation:

- route selection for skill/scenario response delivery should move toward router-owned semantics
- media path selection should reuse the same route-administration vocabulary
- direct `browser <-> member` media should be introduced as a new routed capability, not as a transport-only shortcut

#### Browser sync/runtime integration: `ydoc.service.ts` and media widgets

Current role:

- `ydoc.service.ts` owns sync provider recreation and webspace switching
- media widgets subscribe to semantic channel snapshots and currently may stop loopback on component destroy

Current coupling to remove:

- media widget/component destroy currently implies live media shutdown in some paths
- UI attachment is too close to session ownership

Planned refactor:

- move live media session ownership fully into service state
- keep widgets as observers/controllers, not owners of peer lifetime
- make component unmount close only the local UI binding by default
- require explicit user or policy action to stop live media capture

Expected code changes:

- remove implicit live-media stop from widget destroy paths
- add explicit media session controller APIs for:
  - attach local preview
  - attach remote preview
  - detach preview
  - stop live media session
- keep webspace/sync resync logic isolated from peer/media state except where browser reload naturally resets everything

Minimum acceptance tests:

- opening and closing a media modal does not interrupt an ongoing direct upload
- destroying a media component does not drop the peer unless the page itself unloads

#### Hub WebRTC peer runtime: `services/webrtc/peer.py`

Current role:

- owns hub-side peer instances keyed by `device_id`
- currently replaces the peer unconditionally on every fresh `rtc.offer`
- loops received live tracks back to the browser

Current coupling to remove:

- "new offer => replace existing peer" is used as a safety shortcut
- this makes browser-side renegotiation indistinguishable from full peer replacement

Planned refactor:

- introduce explicit peer-session identity on the signaling path
- keep one active peer session per browser member unless a new session generation is explicitly declared
- allow in-session offer handling for renegotiation on the existing peer
- support explicit supersede only when:
  - protocol/session epoch changed
  - existing peer is failed/closed/unrecoverable
  - operator/debug policy explicitly requests reset
- keep loopback sender/transceiver bookkeeping scoped per subflow instead of relying on full peer replacement cleanup

Expected code changes:

- extend signaling payloads with stable peer session identity and negotiation identity
- teach `handle_rtc_offer(...)` to distinguish:
  - renegotiation for existing peer session
  - replacement of an old peer session
- maintain stronger diagnostics for:
  - active peer session id
  - last negotiation id
  - supersede reason
  - active loopback tracks by subflow

Minimum acceptance tests:

- a renegotiation offer for the current peer session does not close the existing data channels on the hub side
- stale or superseded answers/offers are ignored safely
- repeated start/stop of live media does not accumulate duplicate loopback senders or invalid SDP state

#### Signaling contract and protocol changes

The browser-hub signaling contract will need a small explicit upgrade.

Additions to the signaling model:

- `peer_session_id`
- `negotiation_id`
- optional `media_shape` or equivalent declarative summary of intended live media subflows
- explicit supersede/reset reason when full peer replacement is required

Protocol rules:

- one `peer_session_id` identifies the long-lived direct session
- many `negotiation_id` values may exist within one peer session
- stale answers or ICE for an unknown/superseded session are ignored
- full replacement is explicit, not inferred from the existence of a new offer alone

For `browser <-> member` direct media, the signaling contract will also need:

- target runtime identity, for example `target_node_id`
- explicit media-producer identity, for example `media_session_id` or producer id
- router-visible route intent so signaling can distinguish:
  - browser-hub media
  - browser-member media
  - bounded relay fallback

#### Recommended delivery order

1. browser UI/session ownership cleanup
2. browser transport split between peer shutdown and renegotiation
3. browser semantic-channel API split between peer and media-subflow control
4. router semantic contract for response/media route administration
5. signaling contract upgrade with `peer_session_id`, `negotiation_id`, and target route identity
6. hub-side in-session renegotiation support
7. transceiver-based live media control
8. explicit `browser <-> member` direct media path via hub/root-mediated signaling
9. media-subflow observability and later multi-source expansion

#### Definition of done for the refactor tranche

- direct peer session is long-lived across ordinary media start/stop actions
- semantic channel routing no longer treats media toggles as peer replacement events
- hub no longer assumes every offer means "new peer instance"
- operator surfaces can tell whether an incident is:
  - peer-session failure
  - negotiation failure
  - live-media-subflow failure
  - media-upload-subflow failure
  - route-administration failure such as capability mismatch, policy denial, or producer unavailability

### Work items

- define media signaling authority
- define direct vs relay policy
- keep media readiness outside core control readiness

### Exit criteria

- media path is architecturally isolated from control and sync hardening
- bounded relay upload/playback works through root on a live hub
- direct WebRTC audio/video loopback can be validated end-to-end on a live hub
- operator UI exposes media runtime, relay state, and live loopback status clearly enough for incident/debug use

## Phase 7: Skills and scenarios lifecycle hardening

### Focus

Handle artifact provenance, scenario UX, and runtime lifecycle after the communication model is hardened.

### Work items

- define a first-class artifact model for system skills and scenarios
- separate three lifecycle operations clearly:
  - `source sync`
  - `runtime refresh`
  - `A/B rollout`
- add change classification for skill updates so the system can decide whether `runtime_update` is enough
- make `desktop.scenario.set` transactional and observable with `requested`, `effective`, and `error` state
- define explicit ownership for Yjs subtrees such as `ui`, `data`, `registry`, and desktop-installed artifacts
- surface artifact provenance in diagnostics: `workspace`, `repo_workspace`, `runtime_slot`, `built_in_seed`, `dev`
- preserve scenario install/open state through reload and rebuild of current Yjs projection

### Candidate code areas

- `src/adaos/services/skill/manager.py`
- `src/adaos/services/skill/update.py`
- `src/adaos/services/scenario/manager.py`
- `src/adaos/services/scenarios/loader.py`
- `src/adaos/services/scenario/webspace_runtime.py`
- `src/adaos/services/skills_loader_importlib.py`
- `src/adaos/services/yjs/bootstrap.py`
- `src/adaos/services/yjs/gateway_ws.py`
- `src/adaos/integrations/adaos-client/src/app/runtime/desktop-schema.service.ts`

### Exit criteria

- operator can explain how a skill or scenario update propagates without reading the code
- scenario switch result is observable and not inferred from UI side effects
- remote hubs behave consistently even when local workspace assets are absent
- installed desktop items and current scenario recover correctly after Yjs rebuild or reconnect

## Immediate implementation order

The next coding steps should follow this order:

1. inventory existing hub-root subjects and messages by taxonomy and delivery class
2. isolate route backlog from control backlog in real runtime resources
3. define Class A hub-root flows and add outbox, inbox, and idempotency where required
4. separate root-control and route/session incidents in readiness and diagnostics
5. only then tighten sidecar adoption
6. then move to hub-member, Yjs, and media semantics
7. after the communication phases, harden skills and scenarios lifecycle and scenario UX

## Non-goals for the first iteration

- universal transport abstraction for every edge case
- production-grade media relay
- infinite replay of all traffic
- exactly-once delivery for every message
- replacing all fallback paths before provenance and lifecycle are formalized

The first iteration is about explicit guarantees, provenance, and operational clarity, not maximum theoretical reliability.
