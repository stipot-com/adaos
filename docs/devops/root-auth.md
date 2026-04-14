# Root Authentication Cheatsheet

## Prerequisites

- Ensure the node role in `.adaos/node.yaml` is set to `hub`.
- Install system keyring support when available so refresh tokens can be stored securely.
- Confirm network access to `https://api.inimatic.com` and that the backend service is running with the new owner APIs enabled.

## Owner Login Flow

1. Run `adaos dev root login` on the hub node.
2. Follow the displayed verification link and user code to approve the device (auto-approves in dev environments).
3. The resulting access profile is persisted in the local durable state (SQLite) and refresh tokens are saved in the system keyring (with a durable-state fallback if keyring is unavailable).

## Checking Session Status

- Use `adaos dev root status` to inspect the stored owner profile, token expiry, and hub associations.
- `adaos dev root whoami` performs a live call to Root using the cached/auto-refreshed access token to confirm identity and scopes.

## Managing Hub Registrations

- `adaos dev root sync-hubs` synchronises the owner profile with the hubs registered in Root.
- `adaos dev root add-hub` registers the current hub (or a provided `--hub-id`) with the owner account.
- Only nodes with `role: "hub"` may call these commands; members receive a clear error.

## Hub PKI Enrolment

1. Generate and enrol certificates with `adaos dev hub enroll`.
2. Keys and certificates are stored locally under `.adaos/pki/` (`hub.key`, `hub.crt`, `chain.crt`) with strict permissions.
3. The CSR-only flow keeps private keys on the node; Root returns only the signed certificate and chain.

## File Layout & Tokens

- Bootstrap identity: `.adaos/node.yaml` stores only bootstrap identity and static paths.
- Root auth cache: persisted in local durable state (SQLite), not in `node.yaml`.
- PKI material: `.adaos/pki/{hub.key,hub.crt,chain.crt,node.key,node.crt}`.
- Refresh tokens live in the OS keyring under `adaos-root/api.inimatic.com` (with durable-state fallback when keyring is not available).
