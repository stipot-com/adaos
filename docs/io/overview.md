# I/O Overview

This section describes AdaOS I/O routing and integrations.

- Outgoing UI events: skills publish `ui.notify` and `ui.say`. The RouterService reads `.adaos/route_rules.yaml` and delivers to targets by `io_type` (e.g., `stdout`, `telegram`). Multiple targets can be active; the router attempts each configured target.
- Telegram integration (root backend): receives webhooks, resolves target hub by alias/session/reply/topic, publishes into the bus (NATS) as 	g.input.<hub_id> (modern envelope). For legacy consumers we mirror text to io.tg.in.<hub_id>.text. Outgoing replies are consumed from 	g.output.<bot_id>.> (modern) or io.tg.out (legacy) and delivered to Telegram.\n- NATS subjects:\n  - Modern inbound: 	g.input.<hub_id> — envelope { event_id, kind: 'io.input', ts, payload, meta }.\n  - Legacy inbound (text-only mirror): io.tg.in.<hub_id>.text.\n  - Modern outbound: 	g.output.<bot_id>.chat.<chat_id>.\n  - Legacy outbound: io.tg.out.\n\nSee `io/telegram.md` for full Telegram user flow and commands.

