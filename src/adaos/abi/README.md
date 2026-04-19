# Agent Behavior Interface (ABI)

This folder contains JSON Schemas used by AdaOS for validation and by editors or LLM programmers for structure hints.

- `dcd.v1.schema.json` - device capability descriptor
- `latent.v1.schema.json` - latent state payload
- `lrpc.v1.schema.json` - lightweight RPC messages
- `nb.v1.schema.json` - notebook payload
- `scenario.schema.json` - scenario manifest (`scenario.yaml` or `scenario.json`)
- `skill.schema.json` - skill manifest (`skill.yaml`)
- `webui.v1.schema.json` - skill WebUI contributions (`webui.json`), including staged readiness hints for page, widget, modal, and catalog surfaces

## Current Manifest Runtime Extensions

The ABI includes the typed runtime metadata used by activation-aware workspace orchestration.

### Skill runtime activation

`skill.runtime.activation` describes when a skill should perform expensive work:

- `mode: eager | lazy | on_demand`
- `startup_allowed`
- `background_refresh`
- `when.scenarios_active`
- `when.client_presence`
- `when.webspace_scope`
- `when.webspaces`

### Scenario to skill bindings

`scenario.runtime.skills` lets the scenario own dependency truth:

- `required`
- `optional`

Compatibility note:

- `scenario.depends` remains valid and is treated by runtime code as a legacy alias for required scenario skills.
