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

## Related modules

- `adaos.apps.cli.active_control`
- `adaos.apps.api.*`
- `adaos.sdk.manage.*`
- `adaos.sdk.control_plane`
- `adaos.sdk.data.control_plane`
- `adaos.services.system_model.*`
- `adaos.services.reliability`
- runtime services under `adaos.services.*`
