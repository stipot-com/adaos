# AdaOS Root Service API v1 (MVP)

The persistent `RootAuthorityBackend` exposes a JSON API under the `/v1` prefix.
All successful responses share the same envelope:

```json
{
  "event_id": "evt_...",
  "server_time_utc": "2025-01-01T00:00:00Z",
  "...": "payload fields"
}
```

Errors follow the shape:

```json
{
  "code": "snake_case_error",
  "message": "human readable detail",
  "hint": "optional remediation tip",
  "retry_after": 30,
  "event_id": "evt_...",
  "server_time_utc": "2025-01-01T00:00:00Z"
}
```

## General guarantees

* Every mutating `POST /v1/*` endpoint **requires** an `Idempotency-Key` header.
  Keys are scoped by `(method, path, principal_id, body_hash)` and cached for ten
  minutes. Replays within the TTL produce byte-for-byte identical responses;
  after expiry the service returns `409 idempotency_key_expired`.
* Device and QR registration flows bind the caller's origin metadata via
  HMAC-SHA256 (`origin`, `client_ip_hash`, `ua_hash`, `init_device_thumb`).
* Browser tokens and authenticated calls use holder-of-key proofs (ES256 JWS
  with `kid=jkt` and `cnf.jkt`).
* All timestamps are returned in UTC with millisecond precision.

---

## Device registration flow

### `POST /v1/device/start`
Starts a pairing flow and records anti-relay context hashes.

```http
POST /v1/device/start HTTP/1.1
Content-Type: application/json
Idempotency-Key: dev-start-1

{
  "subnet_id": "sbnt_demo",
  "role": "BROWSER_IO",
  "scopes": ["manage_io_devices"],
  "origin": "https://app.inimatic.com",
  "client_ip": "203.0.113.10",
  "user_agent": "Mozilla/5.0",
  "jwk_thumb": "Gf31...",
  "public_key_pem": "-----BEGIN PUBLIC KEY..."
}
```

```json
{
  "device_code": "dvc_tDqj...",
  "user_code": "K6FZ0H",
  "expires_in": 600,
  "expires_at": "2025-10-05T04:25:00Z",
  "subnet_id": "sbnt_demo",
  "event_id": "evt_...",
  "server_time_utc": "2025-10-05T04:15:00Z"
}
```

### `POST /v1/device/confirm`
Owner approval that binds the request to a subnet, scopes, and key material.

```http
POST /v1/device/confirm HTTP/1.1
Content-Type: application/json
Idempotency-Key: confirm-1
Authorization: Bearer owner-access

{
  "user_code": "K6FZ0H",
  "granted_scopes": ["manage_io_devices"],
  "jwk_thumb": "Gf31...",
  "public_key_pem": "-----BEGIN PUBLIC KEY...",
  "aliases": ["living-room-display"],
  "capabilities": ["display"],
  "origin": "https://app.inimatic.com",
  "client_ip": "203.0.113.10",
  "user_agent": "Mozilla/5.0"
}
```

```json
{
  "device_id": "0199b28f-...",
  "subnet_id": "sbnt_demo",
  "scopes": ["manage_io_devices"],
  "event_id": "evt_...",
  "server_time_utc": "2025-10-05T04:16:00Z"
}
```

### `POST /v1/device/token`
Requester polls with a holder-of-key assertion. The service returns access,
refresh, and short-lived channel tokens.

```http
POST /v1/device/token HTTP/1.1
Content-Type: application/json
Idempotency-Key: poll-1

{
  "device_code": "dvc_tDqj...",
  "assertion": "<ES256 JWS with cnf.jkt thumbprint and nonce=device_code>"
}
```

```json
{
  "device_id": "0199b28f-...",
  "subnet_id": "sbnt_demo",
  "scopes": ["manage_io_devices"],
  "access_token": "atk_...",
  "access_expires_at": "2025-10-05T04:21:00Z",
  "refresh_token": "rtk_...",
  "refresh_expires_at": "2025-10-05T06:15:00Z",
  "channel_token": "chn_...",
  "channel_expires_at": "2025-10-05T04:20:00Z",
  "event_id": "evt_...",
  "server_time_utc": "2025-10-05T04:16:05Z"
}
```

Errors include:
* `authorization_pending` while the owner has not approved.
* `anti_relay_blocked` when origin hashes mismatch.
* `hok_mismatch` if the assertion thumbprint differs from the registered key.

---

## QR pairing flow

### `POST /v1/qr/start`
Returns a session identifier, nonce, and anti-relay metadata for QR onboarding.

### `POST /v1/qr/approve`
Owner approval that issues a BROWSER_IO device idempotently.

Both endpoints enforce `Idempotency-Key` and record hashed origin metadata.

---

## Browser challenge flow

* `POST /v1/auth/challenge` → `{nonce, aud, exp}`
* `POST /v1/auth/complete` (HoK JWS) → token bundle identical to
  `/v1/device/token`

---

## Token lifecycle

* `POST /v1/token/refresh` (not yet exposed via HTTP helper) rotates access and
  channel tokens when presented with a refresh token + HoK assertion.
* `POST /v1/token/introspect` validates a bearer token with HoK proof and
  returns its metadata. Errors: `invalid_token`, `device_revoked`.

---

## Hub & member certificates

### `POST /v1/hub/csr`
Queues owner consent and persists the CSR metadata. Sample request payload:

```json
{
  "csr_pem": "-----BEGIN CERTIFICATE REQUEST...",
  "role": "HUB",
  "subnet_id": "sbnt_demo",
  "scopes": ["manage_io_devices"]
}
```

Response:

```json
{
  "consent_id": "cns_0199...",
  "node_id": "0199b291-...",
  "status": "pending",
  "event_id": "evt_...",
  "server_time_utc": "2025-10-05T04:17:00Z"
}
```

After the owner resolves the consent (`POST /v1/consents/:id/resolve`), the
signed certificate is collected via `GET /v1/consents/:id/certificate` and
contains SAN URIs of the form `urn:adaos:role=HUB`, `urn:adaos:subnet=<id>`,
`urn:adaos:node=<id>`, and `urn:adaos:scopes=<comma-separated>`.

### `GET /v1/consents?status=pending`

Returns the owner's outstanding device/member/hub requests within their
subnet. Each entry includes `{consent_id, requester_id, scopes[], created_at}`.

> **CLI usage**: `adaos dev root consents list` loads the persisted CLI device
> context from `~/.adaos/root-cli/state.json` (or `ADAOS_BASE_DIR` in tests) and
> calls this endpoint with the device's access token. Devices require the
> `manage_members` scope to view member consents and `manage_io_devices` for
> browser/device approvals; lacking both yields a `403 forbidden_scope`.

### `POST /v1/hub/channel/open`

Hubs authenticate with their issued certificate to obtain a five-minute channel
token bound to the SAN metadata. Example response:

```json
{
  "node_id": "0199b291-...",
  "subnet_id": "sbnt_demo",
  "channel_token": "chn_...",
  "expires_at": "2025-10-05T04:20:00Z",
  "event_id": "evt_...",
  "server_time_utc": "2025-10-05T04:17:30Z"
}
```

### `POST /v1/hub/channel/rotate`

Rotates an active channel token (idempotent) and expires the previous token.
Useful when the hub reconnects or approaches expiry.

### `GET /v1/hub/channel`

Verifies the presented channel token and returns `{node_id, subnet_id,
expires_at}`. Errors: `channel_unknown`, `channel_revoked`, `channel_expired`.

### `GET /v1/denylist?subnet_id=sbnt_demo`

Returns the current denylist entries for the subnet. Each element contains the
entity type (`device` or `node`), identifier, reason, creation timestamp, and a
boolean `active` flag.

---

## Directory & alias management

* `GET /v1/devices/:id` → returns aliases, capabilities, scopes, and role for a
  device within the caller's subnet.
* `PUT /v1/devices/:id` (idempotent) → update aliases/capabilities. Any device
  within the subnet may edit aliases; updating capabilities requires the
  `manage_io_devices` scope. Alias collisions return `alias_conflict`.

---

## Revocation & rotation

* `POST /v1/devices/:id/revoke` → immediately revokes tokens and adds the device
  to the denylist.
* `POST /v1/devices/:id/rotate` → rotates HoK thumbprints using a HoK assertion.

---

## Audit export

`GET /v1/audit?subnet_id=sbnt_demo`

Returns NDJSON where each line is a signed record:

```
{"event_id":"evt_...","trace_id":"trc_...","subnet_id":"sbnt_demo",...,"signature":"c5f..."}
```

Records can be verified with `AuditExport.verify(hmac_key)`; tampering yields
`audit_corrupted`.

---

## Error examples

* Missing idempotency key:

```json
{
  "code": "missing_idempotency_key",
  "message": "Idempotency-Key header is required.",
  "event_id": "evt_...",
  "server_time_utc": "2025-10-05T04:15:00Z"
}
```

* Holder-of-key mismatch:

```json
{
  "code": "hok_mismatch",
  "message": "Holder-of-key thumbprint mismatch.",
  "event_id": "evt_...",
  "server_time_utc": "2025-10-05T04:16:05Z"
}
```

* Rate limit exceeded:

```json
{
  "code": "rate_limited",
  "message": "Request rate exceeded for this endpoint.",
  "retry_after": 12,
  "event_id": "evt_...",
  "server_time_utc": "2025-10-05T04:18:30Z"
}
```

