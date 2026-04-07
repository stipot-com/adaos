# SDK Control Plane

In the current codebase, the control plane is split across:

- the local HTTP API
- CLI commands that resolve and call that API
- SDK and service helpers that provide stable operations for higher-level flows

## What is implemented today

- skill install, update, runtime prepare, and activation flows
- scenario install and sync flows
- node status, reliability, join, role, and member-update flows
- service supervision and issue reporting
- webspace and desktop control for Yjs-backed state
- canonical self-object access via SDK-first control-plane helpers
- canonical skill/scenario object access via SDK-first helpers before widening the external API
- canonical reliability projection access for LLM and skills, with runtime component objects and action metadata
- canonical neighborhood projection access over subnet-directory peers, root connectivity, and nearby capacity snapshots
- canonical workspace, profile, browser-session, device, quota, and local capacity objects through SDK-first helpers
- canonical kind and relation registries under `adaos.services.system_model.model` so SDK, API, and LLM projections share the same vocabulary
- shared governance and action-role defaults so SDK-facing objects carry owner, visibility, and role hints consistently
- local inventory projection that combines node, workspace, browser, device, skill, scenario, capacity, and selected reliability-derived root/quota objects for LLM-oriented reasoning

## Relationship to Root MCP Foundation

The current SDK control-plane layer is the nearest implemented internal precursor to the future `MCP Development Surface`.

Phase 0 and early Phase 1 architectural guidance is:

- SDK remains the primary internal contract surface for skills and local LLM workflows
- root-hosted MCP should expose root-curated descriptors over services, schemas, manifests, and selected SDK-derived metadata, not a direct public bridge into SDK internals
- external HTTP facades should stay narrower than SDK where possible
- future `Root MCP Foundation` should build on `adaos.sdk.core.exporter`, `adaos.services.system_model.*`, manifest schemas, template metadata, and managed-target registries
- external MCP clients should speak to `root` with a scoped client model such as `root_url + subnet_id + access_token + zone`, not import SDK directly
- current Phase 1 root MCP read surfaces already expose descriptor and managed-target endpoints, but they remain intentionally narrower than SDK and are guarded by root-side capability policy

In other words, AdaOS should remain `SDK-first` inside the platform, while MCP becomes the governed root-hosted agent-facing entrypoint over root-curated descriptors and managed operational surfaces.

## Related modules

- `adaos.apps.cli.active_control`
- `adaos.apps.api.*`
- `adaos.sdk.manage.*`
- `adaos.sdk.core.exporter`
- `adaos.sdk.control_plane`
- `adaos.sdk.data.control_plane`
- `adaos.services.system_model.*`
- `adaos.services.reliability`
- runtime services under `adaos.services.*`
