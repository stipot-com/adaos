# Codex Test-Hub MCP

This document describes the current flow for connecting Codex in VS Code to a test hub through AdaOS Root MCP.

## Recommended VS Code Setup

For the current VS Code Codex workflow, prefer direct remote MCP over the root-published HTTP endpoint:

- MCP URL: `https://<zone>.api.inimatic.com/v1/root/mcp`
- bearer token env var: `ADAOS_ROOT_MCP_AUTH`

In practice:

1. Use `infra_access_skill` to issue a fresh MCP session lease for the target.
2. Copy the returned `mcp_http_url`.
3. Store the returned `access_token` in the OS environment as `ADAOS_ROOT_MCP_AUTH`.
4. Point the VS Code Codex MCP server config at that URL and env var.

On Windows:

```powershell
setx ADAOS_ROOT_MCP_AUTH "mcp_..."
```

Important:

- `setx` updates the user environment for new processes only.
- already running VS Code, Codex, terminals, and MCP helper processes keep the old bearer
- after rotating the bearer, fully restart VS Code if Codex keeps using the previous token

If you want to verify the issued bearer before wiring Codex, test it directly:

```powershell
curl -i https://ru.api.inimatic.com/v1/root/mcp/foundation `
  -H "Authorization: Bearer $env:ADAOS_ROOT_MCP_AUTH"
```

and:

```powershell
curl -i https://ru.api.inimatic.com/v1/root/mcp `
  -H "Authorization: Bearer $env:ADAOS_ROOT_MCP_AUTH" `
  -H "Content-Type: application/json" `
  -d '{"jsonrpc":"2.0","id":"1","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
```

## Local Bridge MVP

The current implementation is intentionally a local `stdio` bridge:

`Codex in VS Code -> local bridge -> RootMcpClient -> Root MCP API -> managed test hub`

This keeps SDK internals private, avoids exposing a fake direct SDK path, and fits the current Phase 1 state of `Root MCP Foundation`.

## What This MVP Covers

- scoped Codex connection to one managed target, normally `hub:<subnet_id>`
- read-first operational methods for the target
- token issuance through the target's published `infra_access_skill` token-management surface
- profile and token files stored under the workspace-local `.adaos/mcp/` directory

The bridge currently exposes these tools:

- `foundation`
- `get_architecture_catalog`
- `get_sdk_metadata`
- `get_template_catalog`
- `get_public_skill_registry`
- `get_public_scenario_registry`
- `list_managed_targets`
- `get_managed_target`
- `get_operational_surface`
- `get_status`
- `get_runtime_summary`
- `get_activity_log`
- `get_capability_usage_summary`
- `get_logs`
- `run_healthchecks`
- `recent_audit`
- `get_yjs_load_mark_history`
- `get_yjs_logs`
- `get_skill_logs`
- `get_adaos_logs`
- `get_events_logs`
- `get_subnet_info`
- `get_subnet_analysis_health`

## Why This Is a Local Bridge

Codex can register MCP servers directly, but the current AdaOS root surface is still a Root MCP API foundation, not a full remote MCP transport. In addition, AdaOS scope is currently represented by `root_url + subnet_id + zone + bounded access token`, while Codex's native remote HTTP MCP setup is better suited to `url + bearer`.

For that reason, the current MVP uses a local bridge process started by Codex. The bridge reads a workspace-local profile and token file, then translates MCP tool calls into `RootMcpClient` calls.

## Prerequisites

Before setup, make sure:

- the workspace is connected to the intended root
- the target test hub is visible as a managed target on root
- `infra_access_skill` is published by the target if you want operational tools
- `adaos dev root login` has been completed, or you have `ROOT_TOKEN` / `ADAOS_ROOT_TOKEN`

For `get_logs` and `run_healthchecks`, the target must currently publish `infra_access_skill` with `execution_mode=local_process`.

## Local Bridge Setup

### 1. Prepare the Codex bridge profile

Run:

```powershell
adaos dev root mcp prepare-codex
```

By default this will:

- infer `target_id` as `hub:<subnet_id>`
- issue a bounded MCP access token for audience `codex-vscode`
- write a profile file under `.adaos/mcp/adaos-test-hub.profile.json`
- write a token file under `.adaos/mcp/adaos-test-hub.token`
- print the exact `codex mcp add ...` command to register the bridge

Useful options:

```powershell
adaos dev root mcp prepare-codex --target-id hub:test-subnet --ttl-seconds 14400
adaos dev root mcp prepare-codex --owner-token $env:ROOT_TOKEN
adaos dev root mcp prepare-codex --apply-codex
```

`--apply-codex` updates `~/.codex/config.toml` automatically.

### 2. Register the bridge in Codex

Copy the command printed by `prepare-codex`, or run the same shape manually:

```powershell
codex mcp add adaos-test-hub --env ADAOS_MCP_PROFILE=D:\git\adaos\.adaos\mcp\adaos-test-hub.profile.json -- D:\git\adaos\.venv\Scripts\python.exe -m adaos dev root mcp serve
```

The important parts are:

- `ADAOS_MCP_PROFILE`
  points the bridge to the workspace-local profile JSON
- the Python command starts the local bridge over `stdio`

The token itself is not stored in `~/.codex/config.toml`; the bridge reads it from the token file referenced by the profile.

## Workspace Directories

The local bridge now uses explicit workspace-local directories with separate purposes:

- `.adaos/mcp/`
  bridge profile and token files used by Codex
- `.adaos/state/root_mcp/`
  Root MCP state and cache such as descriptor cache, session registry, and control reports
- `.adaos/logs/`
  local AdaOS process logs for the current machine; this is not an MCP response cache

### 3. Verify the Codex registration

```powershell
codex mcp list
codex mcp get adaos-test-hub
```

Then open Codex in VS Code and ask it to inspect the target. A good first prompt is:

```text
Use the AdaOS test-hub MCP tools to inspect the target operational surface, status, and runtime summary.
```

In Codex tool traces you should see namespaced tools such as:

- `mcp__adaos-test-hub__get_operational_surface`
- `mcp__adaos-test-hub__get_status`
- `mcp__adaos-test-hub__get_runtime_summary`

## Rotation and Refresh

To rotate the token, run `prepare-codex` again:

```powershell
adaos dev root mcp prepare-codex --apply-codex
```

The bridge reads the token file on each call, so token rotation does not require rewriting the server definition if the profile path stays the same.

To remove the Codex registration:

```powershell
codex mcp remove adaos-test-hub
```

## Troubleshooting

### Target is not registered

The command defaults to `--ensure-target`, so it can create a minimal test-target record on root. If the real target state still does not show up, verify control reports and target registration.

### Token issuance fails

`prepare-codex` uses the target-scoped `hub.issue_access_token` path. If this fails, the target usually has not published `infra_access_skill` token management yet.

Check:

- `get_operational_surface`
- target control reports on root
- `infra_access_skill` draft or installed state on the hub

### Logs or healthchecks fail

Those methods currently depend on `execution_mode=local_process`. If the target publishes only `reported_only`, status and observability tools still work, but bounded execution tools do not.

The dedicated log tools such as `get_adaos_logs`, `get_events_logs`, `get_skill_logs`, and `get_yjs_logs` now default to `scope=subnet_active`. This is intended to avoid empty `root_local` answers masking a healthy hub-root observability path. Use `scope=root_local` only when you intentionally want logs from the local machine hosting the bridge or root service.

For Phase 7 subnet analysis, prefer `get_subnet_analysis_health` before deeper investigation. It summarizes whether current snapshot, session-registry, audit, and `subnet_active` log channels are trustworthy enough for memory or incident analysis.

## Current Boundaries

This MVP intentionally does not yet provide:

- direct remote MCP transport from root to Codex
- arbitrary shell access
- unrestricted deploy or rollback operations
- a public SDK import path for external MCP clients

Those remain later phases of `Root MCP Foundation`.
