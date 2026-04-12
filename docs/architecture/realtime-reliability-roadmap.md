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

## Current status: 2026-03-21

### Done

- architecture documents for channel semantics, authority, hub-root protocol, and transport ownership are in place
- runtime reliability model is represented in code and exposed through `GET /api/node/reliability`
- `adaos node reliability` surfaces readiness, degraded matrix, and channel diagnostics
- Infra State shows realtime summary and transport diagnostics through Yjs-backed UI
- runtime now exposes canonical channel overview entries for `hub_root`, `hub_root_browser`, and `browser_hub_sync`
- runtime now exposes `hub_root_transport_strategy` with current transport, candidate list, recent attempts, reconnect/failure history, and active hypothesis parameters
- CLI and Infra State now surface the current hub-root transport strategy instead of only the last readiness bit
- detailed channel trace is no longer a default console behavior; summary/incident output remains visible while deep console trace is explicit opt-in
- channel stability is now assessed from incidents and transport churn, not only from the last connected snapshot
- repo workspace fallback exists for built-in skills, scenarios, and `webui.json`
- built-in fallback for `web_desktop` restores the return path from scenario views when scenario assets are missing on a hub
- canonical runtime store for skill-local env and memory moved to `.runtime/<skill>/data/db/skill_env.json`

### In progress

- hub-root readiness is observable, but delivery guarantees are not yet enforced by an explicit Class A protocol layer
- route and root-control incident classes still need clearer separation
- transport strategy is now visible, but automatic policy-driven transport switching is not yet the default runtime behavior
- sidecar ownership boundary is documented, but not yet the default hardened runtime path
- local process/update supervision is still coupled to runtime lifecycle rather than a separate supervisor authority
- Yjs ownership boundaries for desktop and scenario state are still implicit

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
Sidecar lifecycle is also independently observable and restartable through the local control API and CLI, so transport isolation is no longer tied to a full hub restart.
This completion is intentionally transport-only: the sidecar owns the `hub_root` NATS transport lifecycle, but does not yet own route tunnel transport, Yjs sync transport, or media transport.

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
Current MVP coverage now includes slot-first validation, explicit root-promotion states, an explicit `root restart in progress` attempt stage after root promotion, forced shutdown recovery for hung runtime restarts, one queued subsequent transition after an in-flight transition, minimum-interval scheduling for normal update requests, operator-driven defer for planned/countdown updates, a browser-shell transition badge, routed read-only supervisor transition polling on `/hubs/<id>/api/supervisor/public/update-status`, a canonical supervisor runtime object in the control-plane model so Infrascope/overview surfaces can project transition state as an operator runtime instead of only a transport outage, and a slot-bound runtime-port model with an explicit supervisor-side warm-switch admission decision (`warm_switch` vs `stop_and_switch`) based on reserved A/B ports and local memory headroom.
The remaining gap is full candidate-runtime prewarm and fast cutover: the browser/control path can now see which mode supervisor selected and which candidate URL/port is reserved, but the actual dual-runtime launch/cutover path still needs to be completed so low-downtime switching becomes operational rather than only planned/observable.

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
- align sidecar lifecycle under supervisor without turning sidecar into protocol or update authority
- migrate installed skill runtimes as an explicit core-update subflow rather than assuming old interpreter dependencies remain valid
- persist per-skill migration diagnostics (`prepare` / `test` / `activate` / `rollback` / `deactivate`) in core-update results
- surface skill migration failures and selective post-commit deactivations in Infra State and Infrascope
- keep supervisor transition state visible in canonical operator projections (`active_runtimes`, health strips, recent changes) rather than only in ad-hoc browser badges
- separate runtime liveness from listener/API readiness in supervisor-visible status
- surface the active managed runtime command/source in supervisor diagnostics
- surface active-slot structure validation in supervisor diagnostics so broken slot layouts fail explicitly
- harden diagnostic skills so Yjs-backed operator surfaces keep the last usable local snapshot during transient control-plane file failures
- keep browser-facing update visibility alive through supervisor status/polling while `/ws` and `/yws` reconnect during slot restart
- expose a read-only browser-safe supervisor transition surface so restart/update state is not collapsed into generic `offline`
- distinguish browser-facing `hub restarting`, `update applying`, `rollback`, `root promotion pending`, `root restart in progress`, and `update failed` from ordinary transport reconnect state
- surface `planned`, `deferred`, minimum-window scheduling, and queued follow-up transition state through that same browser-safe/read-only supervisor surface
- extend the routed read-only supervisor transition surface across every browser deployment topology, not only the default `/hubs/<id>/api/...` entry path
- reserve stable runtime ports per slot so supervisor can reason about `active` and `candidate` runtimes explicitly
- add a memory gate that decides when dual-runtime warm-switch is safe and when supervisor must fall back to stop-and-switch
- surface `transition_mode`, candidate runtime URL/port, and warm-switch admission reason in operator and browser-safe status
- complete candidate prewarm and fast cutover only after those control-plane signals are stable

### Candidate code areas

- `src/adaos/apps/autostart_runner.py`
- `src/adaos/services/core_update.py`
- `src/adaos/services/autostart.py`
- `src/adaos/apps/cli/commands/setup.py`
- `src/adaos/apps/cli/commands/node.py`
- future `src/adaos/apps/supervisor.py`

### Exit criteria

- update status remains visible while runtime is stopped
- stale `restarting` / `applying` states resolve deterministically
- rollback decision is owned by supervisor logic rather than only runtime-side best effort
- sidecar remains transport-only and does not absorb process/update authority
- operators can identify which installed skill failed during a core migration and at which stage
- operators can distinguish `slot validation`, `root promotion pending`, and `root restart in progress` from supervisor-visible state
- browser header/status surfaces can distinguish controlled supervisor-managed restart/update transitions from plain hub offline or transport loss
- routed browser sessions can continue reading live supervisor transition state even while runtime `/api`, `/ws`, and `/yws` are unavailable
- operators can tell whether the next transition is planned as `warm_switch` or `stop_and_switch`, and why

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

### Checkpoint reached

- hub-side YStore runtime now exposes bounded log and snapshot+diff state for operator diagnostics
- browser sync now has an explicit resync path and runtime snapshot instead of scattered provider recreation logic
- node reliability / hub-root status surface Yjs sync runtime alongside transport and protocol state
- browser header now exposes a manual Yjs resync action, separate from scenario reseed/reload
- browser sync runtime now separates document recovery from ephemeral awareness state
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
