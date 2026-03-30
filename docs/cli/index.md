# CLI

The `adaos` command is the main operator and developer entry point.

## Core command groups

- `api`: start, stop, and restart the local FastAPI control server
- `skill`: install, validate, run, update, activate, and supervise skills
- `scenario`: install, validate, run, test, and compare scenarios
- `node`: inspect role, readiness, reliability, Yjs webspaces, and member operations
- `hub`: manage join codes and hub-root diagnostics
- `autostart`: manage OS startup integration and core-update actions
- `monitor`: stream events and SSE diagnostics
- `runtime`: inspect local runtime state
- `secret`: store and retrieve secrets
- `dev`: Root and Forge-style developer workflows
- `interpreter`: NLU datasets, parsing, and training utilities

## Common examples

```bash
adaos api serve
adaos skill list
adaos skill validate weather_skill
adaos scenario list
adaos node status --json
adaos hub join-code create
adaos autostart status
```

## Execution model

Many commands support two paths:

- direct local execution through services
- API-backed execution against the active control server

This is especially visible in skill, node, and service-management commands.
