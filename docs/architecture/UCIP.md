# Метод: UCIP (use-case + spec first, без рантайма)

Каждый UCIP — один PR и одно поведение end-to-end. Мы не требуем запуск тестов; мы требуем **артефакты** и **изменения по путям**.

## Структура UCIP

1. **Narrative (Given/When/Then)** — 5–8 строк.
2. **API contract (конкретно):** маршруты, методы, заголовки, тела запрос/ответ (JSON), коды ошибок.
3. **DB delta:** таблицы/поля/индексы и SQL-миграция.
4. **Frontend contract:** имена файлов/компонентов, пропсы, ожидаемые состояния UI.
5. **Backend touch list:** какие файлы править (точные пути), какие новые добавить.
6. **Non-goals:** что не делать в этом PR.
7. **PR deliverables:** список файлов/артефактов, OpenAPI дифф, curl-сценарии, скрин макета (если нужно).
8. **Review checklist:** 8–10 бинарных пунктов (idempotency, error schema, alias policy и т.п.).
9. **Diff budget:** лимит строк и файлов.

## Лёгкие CI-предохранители (без рантайма)

* **Path coverage:** PR должен изменять `src/backend/`, `frontend/`, `docs/api-v1.md`, `migrations/`.
* **OpenAPI check:** файл `docs/openapi.v1.yaml` должен быть обновлён; PR содержит «contract diff» секцию.
* **SQL presence:** новая миграция в `migrations/` при изменении API, где есть storage.
* **Lint/format only:** без запуска интеграций.

---

# Готовый UCIP для Codex (пример: UC-2 Owner browser device login)

> **Title:** UC-2 — Owner browser device login (Device Flow + HoK)
> **Goal (single PR):** Implement the end-to-end browser login flow on **backend + frontend**, per the contracts below. No mocks; produce concrete API/DB/docs/UI artifacts.

## 1) Narrative

**Given** the browser has a WebCrypto key (represented as JWK in dev mode),
**When** it performs the device flow (`/v1/device/start` → owner confirm → `/v1/auth/challenge` → `/v1/auth/complete`),
**Then** it receives short-lived access (5–10m) and refresh (≤4h) tokens bound to the key (HoK), and subsequent HoK-signed calls succeed. Reuse of `device_code` after `used_at` yields `already_used`.

## 2) API Contract (must be implemented exactly)

Base path: `/v1`
All mutating POST require header: `Idempotency-Key: <uuid>` → repeat within TTL returns **byte-identical** body+status.

* `POST /v1/device/start`
  Req headers: `Idempotency-Key`
  Req body: `{ "browser_jwk_thumb":"<jkt>" }`
  Resp 200:

  ```json
  { "data": { "device_code":"...", "user_code":"...", "expires_in": 300 },
    "event_id":"...", "server_time_utc":"2025-10-05T15:00:00Z" }
  ```

  Errors: `missing_idempotency_key` (400)

* `POST /v1/device/confirm` (OWNER-authorized call)
  Req body: `{ "device_code":"...", "scopes":["emit_event"] }`
  Resp 200 empty `data` with envelope.
  Errors: `invalid_device_code` (400), `already_used` (409), `expired` (410)

* `POST /v1/auth/challenge`
  Req body: `{ "aud":"subnet:<id>" }`
  Resp 200: `{ "data": { "nonce":"...", "exp": 1730000000 },
               "event_id":"...","server_time_utc":"..." }`

* `POST /v1/auth/complete`
  Req body: **JWS** (header `kid=<jkt>`, payload `{nonce,aud,ts,"cnf":{"jkt":"<jkt>"}}`)
  Resp 200:

  ```json
  { "data": { "access":"<jwt|opaque>", "refresh":"<opaque>", "access_expires_in":600 },
    "event_id":"...","server_time_utc":"..." }
  ```

  Errors: `hok_mismatch` (401), `clock_skew_detected` (400)

**Unified error JSON:** `{ "code":"snake_case", "message":"...", "hint":"?", "retry_after":? ,
"event_id":"...", "server_time_utc":"..." }`

## 3) DB Delta (SQLite; add migration)

New/updated tables:

* `device_codes(device_code TEXT PK, user_code TEXT, browser_jwk_thumb TEXT, status TEXT, created_at INT, expires_at INT, used_at INT)`
* `tokens(token_id TEXT PK, device_id TEXT, kind TEXT, expires_at INT, created_at INT)`
* `idempotency_cache(key TEXT, method TEXT, path TEXT, principal_id TEXT, body_hash TEXT, status TEXT, status_code INT, headers_json TEXT, body_json TEXT, created_at INT, expires_at INT, event_id TEXT, server_time_utc TEXT, PRIMARY KEY(key,method,path,principal_id,body_hash))`
  Indexes: `device_codes(expires_at)`, `idempotency_cache(expires_at)`

## 4) Frontend Contract

Files to create/update:

* `frontend/src/lib/api/auth.ts` — typed clients for the four routes above; HoK signing helper (JWS) stub with pluggable key source.
* `frontend/src/modules/auth/DeviceLogin.tsx` — minimal UI that:

  1. Calls `/v1/device/start` (shows `user_code`),
  2. Waits for external confirm (button “I confirmed” → proceeds),
  3. Calls challenge+complete and stores tokens (simulate IndexedDB via wrapper),
  4. Shows “authorized” state.

**UI states:** idle → starting → waiting_confirm → completing → authorized → error(message)

## 5) Backend Touch List (must modify these paths)

* `src/backend/routers/device.py` (new or extended handlers for the 4 routes)
* `src/backend/persistence/sqlite.py` (DAO + migrations)
* `src/backend/auth/hok.py` (JWS verify: `kid=jkt`, payload cnf.jkt, ±90s skew tolerance)
* `docs/api-v1.md` (add route examples above)
* `docs/openapi.v1.yaml` (spec for 4 routes)

## 6) Non-goals

* No UI for OWNER approval in this PR (assume it exists; stubbed).
* No WSS/channel tokens (это в другом UC).
* No offline flows.

## 7) PR Deliverables

* Updated backend routes/DAO/auth code.
* Updated frontend API client + `DeviceLogin.tsx`.
* Migration SQL in `migrations/20251005_uc2.sql`.
* `docs/openapi.v1.yaml` and `docs/api-v1.md` with exact examples.
* `docs/uc2_curl_examples.md` with 3 curl sequences (start, confirm, complete).
* **Contract diff** section in PR description (what changed in OpenAPI).

## 8) Review Checklist (reject PR if any “No”)

* [ ] All four routes exist under `/v1/*` and match payloads above.
* [ ] Mutating POSTs require `Idempotency-Key` and replay identically within TTL.
* [ ] Success responses include `server_time_utc` & `event_id`; errors follow unified schema.
* [ ] DB migration present; tables/indices match spec.
* [ ] Frontend shows 5 UI states and can complete flow with stubbed confirm.
* [ ] HoK verify checks `kid=jkt` and `cnf.jkt`; skew ±90s.
* [ ] OpenAPI & docs updated.

## 9) Diff budget

≤ 500 added lines; ≤ 12 files changed. No new top-level dirs.

---

# Как этим пользоваться

* Для каждого UC выпускаем такой UCIP.
* В GitHub добавляем лёгкие CI-чеки: затронутые пути + наличие миграции + OpenAPI diff.
* В описании PR Codex обязан вставить «Contract diff» и «Checklist».
