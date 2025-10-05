# Patch 1 — Persistence, Idempotency, Unified Errors (Verification Report)

## Test Matrix

| Test suite | Command | Result |
| --- | --- | --- |
| Foundational idempotency & persistence | `PYTHONPATH=src pytest tests/integration/test_foundation.py -q` | Pass |
| Schema & migration coverage | `PYTHONPATH=src pytest tests/integration/test_migrations.py -q` | Pass |
| Root backend unit checks | `PYTHONPATH=src pytest tests/services/root/test_backend.py -q` | Pass |
| End-to-end HoK & CSR flow | `PYTHONPATH=src pytest tests/integration/test_root_mvp_full.py -q` | Pass |
| Hub bootstrap & channel rotation | `PYTHONPATH=src pytest tests/integration/test_hub_agent.py -q` | Pass |
| Browser HoK flows (owner + IO) | `PYTHONPATH=src pytest tests/integration/test_browser_flows.py -q` | Pass |
| Owner CLI workflows | `PYTHONPATH=src pytest tests/integration/test_cli_owner_workflows.py -q` | Pass |
| Channel security regression | `PYTHONPATH=src pytest tests/integration/test_channel_security.py -q` | Pass |
| Full suite regression | `PYTHONPATH=src pytest -q` | Pass |

## Implementation Notes

* Added HMAC-SHA256 context binding for origin, client IP, and user-agent values using the configured `context_hmac_key` fallback to `hmac_audit_key`.
* Reworked the idempotency cache schema to capture `(key, method, path, principal_id, body_hash)` scope, persisted responses, expiry timestamps, and commit markers for safe replay under concurrency.
* Updated the backend idempotency handler to enforce TTL expiry with deterministic `409 idempotency_key_expired` errors and to guarantee byte-identical responses across replays.
* Normalised response and error envelopes to emit RFC3339 UTC timestamps with a `Z` suffix.
* Added a global idempotency lock to avoid SQLite threading misuse and guarantee consistent replay under parallel calls.
* Expanded persistence to cover subnets, nodes, alias tables, denylist, CSR queues, issued certificates, browser challenges, rate counters, dedicated hub channel tokens, and denylist subnet attribution.
* Adjusted consent listing/approval to validate owner device scopes (`manage_members` vs `manage_io_devices`) and to permit multiple owner devices per subnet via scope-based filtering.

## Database Verification

* `idempotency_cache` columns: `id`, `idempotency_key`, `method`, `path`, `principal_id`, `body_hash`, `response_json`, `status_code`, `headers_json`, `event_id`, `server_time_utc`, `created_at`, `expires_at`, `committed`.
* Unique index `idempotency_cache_unique` covers `(idempotency_key, method, path, principal_id, body_hash)`.
* Expiry index `idx_idempotency_expires` enforces efficient cleanup scanning.
* `hub_channels` table persists `{node_id, token, subnet_id, expires_at, created_at, rotated_at, revoked}` with expiry index `idx_hub_channels_expires`.
* `denylist` now includes `subnet_id` to scope revocation broadcast per owner subnet.

## Outstanding Items

* No deviations observed for Patch 1 requirements. Hardened MVP extends coverage to QR pairing, HoK browser auth, alias policy, audit export, and CSR signing as validated by `test_root_mvp_full`.

