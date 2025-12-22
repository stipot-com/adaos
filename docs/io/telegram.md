# Telegram Multi‑Hub Routing (MVP)

This document explains how a single Telegram chat can be linked to multiple hubs (subnets) with a "current subnet" model and explicit alias addressing.

## Concepts

- Binding: `chat_id ↔ hub_id` stored as `tg_bindings` with unique `alias` per chat.
- Session: `tg_sessions.current_hub_id` keeps the current subnet for a chat.
- Topic binding: `tg_topics` maps group threads (topics) to a hub.
- Message log: `tg_messages` stores thin logs: `(tg_msg_id, chat_id, hub_id, alias, via)`.
- Link (legacy outbound): `tg_links` stores `hub_id → (bot_id, chat_id)` for routes that lack `chat_id`.

## Resolver priority

`explicit > reply > topic > session > default > need_choice`.

- Explicit alias: leading `@alias` or `#alias` (1–32 chars `[A-Za-z0-9._-]`). The alias is stripped from text before forwarding.
- Reply: reply to a message previously sent by the target hub.
- Topic: group thread (topic) mapping.
- Session: previously selected with `/use`.
- Default: one binding per chat can be marked as default.
- Need choice: bot prompts the user to pick an alias.

## Commands

- `/use <alias|hub_id>` — set current subnet for the chat.
- `/current` — show current (`✅`) and default (`⭐`).
- `/list` — show bindings with inline buttons:
  - Make current, make default.
- `/default <alias>` — mark default.
- `/alias <hub|alias> <new_alias>` — rename alias (unique per chat).
- `/unlink <alias>` — remove binding (also clears topic bindings for that hub).
- `/bind_here <alias>` — in group topics: bind current thread to a hub.
- `/unbind_here` — unbind current thread.
- `/help` — quick reference.

## Deeplink `/start`

- `start=bind:<hub_id>` — create/confirm binding. Alias is auto‑assigned (`hub`, `hub-2`, ...).
- `start=use:<alias>` — set current subnet.

## NATS Protocol

Inbound from Telegram to hub (modern):
- Subject: `tg.input.<hub_id>`
- Envelope: `{ event_id, kind: 'io.input', ts, payload: ChatInputEvent, meta: { bot_id, hub_id, trace_id } }`

Inbound mirror for legacy hubs (text-only):
- Subject: `io.tg.in.<hub_id>.text`
- Payload: `{ text, chat_id, tg_msg_id, route, meta }`

Outbound from hub to Telegram:
- Modern: `tg.output.<bot_id>.chat.<chat_id>` with `{ target, messages[] }` (see types in backend)
- Legacy: `io.tg.out` with `{ target, messages[] }`

## Deployment

Environment variables (root backend):
- `PG_URL=postgres://user:pass@postgres:5432/inimatic`
- `TG_BOT_TOKEN=...`
- `TG_ROUTER_ENABLED=1`
- `TG_WEBHOOK_PATH_PREFIX=/io/tg`
- NATS core or WS:
  - `NATS_URL=nats://nats:4222` (internal)
  - `NATS_ISSUER_SEED/ NATS_ISSUER_PUB/ NATS_AUTH_PASSWORD` (WS+Auth Callout)

Environment variables (hub, WS client):
- `NATS_WS_URL=wss://nats.inimatic.com` (path is controlled by `WS_NATS_PATH`, default is `/` in our docker-compose)
- `NATS_USER=hub_<hub_id>`
- `NATS_PASS=<hub_nats_token>`
- `HUB_ID=<hub_id>`

Docker Compose:
- NATS server enables WebSocket (mounted at `WS_NATS_PATH`, default `/`) and Auth Callout to backend `/internal/nats/authz`.
- Postgres service provides storage for Telegram tables.

Schema is applied automatically by the backend at startup/webhook.

## User Flow

1) Pair a hub: open bot deeplink or call `/io/tg/pair/create` → `/io/tg/pair/confirm`.
2) Set default/current alias: `/default <alias>`, `/use <alias>`.
3) Send `@alias hello` to route to a specific hub; reply to route back to the same hub.
4) Hubs publish to `io.tg.out` to reply; messages appear with `🔹[alias]` prefix.
