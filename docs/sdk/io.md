# SDK IO

SDK module: `adaos.sdk.io`

## Output (unified)

These helpers publish events onto the local bus. They do not write to Yjs directly.

- `io.out.chat.append(text, from_='hub', _meta={...})`
  - RouterService projects into `data.voice_chat.messages` of the target webspace.
- `io.out.say(text, lang='ru-RU', _meta={...})`
  - RouterService projects into `data.tts.queue` of the target webspace.
- `io.out.media.route(need='scenario_response_media', _meta={...})`
  - RouterService normalizes the media route intent/contract and projects it into `data.media.route` of the target webspace.

### Routeless skills via `_meta` context

When a tool is invoked with a payload that contains `_meta`, AdaOS automatically
sets an execution context so `io.out.*` helpers inherit it.

This means a skill can stay stateless and "routeless":

- no direct Yjs writes
- no explicit `_meta=...` in `chat_append()` / `say()` (unless you want to override)

RouterService also supports broadcasting by setting `_meta.webspace_ids = ['w1', 'w2', ...]`.
For dynamic runtime routing without changing skills, you can also set `_meta.route_id`
and configure targets in Yjs: `data.routing.routes[route_id] = { webspace_ids: [...] }`.

## WebIO data contracts (MVP)

### `data/voice_chat`

`data.voice_chat` is a JSON object:

```
{
  "messages": [
    { "id": "m.123", "from": "user|hub", "text": "…", "ts": 1730000000.0 }
  ]
}
```

### `data/tts`

`data.tts` is a JSON object:

```
{
  "queue": [
    { "id": "t.123", "text": "…", "ts": 1730000000.0, "lang": "ru-RU", "voice": "…", "rate": 1.0 }
  ]
}
```

### `data/media`

`data.media` is a JSON object.
Today the router-owned route contract lives under `data.media.route`:

```
{
  "route": {
    "route_intent": "scenario_response_media|live_stream|upload|playback",
    "preferred_route": "local_http|root_media_relay|hub_webrtc_loopback|member_browser_direct",
    "active_route": "local_http|root_media_relay|hub_webrtc_loopback|member_browser_direct|null",
    "producer_authority": "hub|member|shared|none",
    "producer_target": { "kind": "hub|member", "member_id": "...", "webspace_id": "..." },
    "selection_reason": "....",
    "degradation_reason": "....",
    "member_browser_direct": {
      "possible": true,
      "admitted": false,
      "ready": false,
      "reason": "member_browser_direct_policy_not_admitted_yet",
      "candidate_member_total": 1,
      "browser_session_total": 2
    },
    "monitoring": {
      "watch_signals": ["..."],
      "observed_failure": null
    },
    "route_administrator": "router",
    "updated_at": 1730000000.0
  }
}
```

`io.out.media.route(...)` may publish either:

- a minimal route intent with capability/ability hints, which the router normalizes
- a precomputed route contract, which the router re-targets to the destination webspace and republishes as browser-visible state

## Voice (local mock)

- `io.voice.stt.listen(timeout='20s')`
- `io.voice.tts.speak(text)`

## Web STT (frontend)

`ui.voiceInput` supports pluggable STT providers via `widget.inputs.stt`:

- `provider: 'browser'` — Web Speech API (`SpeechRecognition`) with partials
- `provider: 'hub'` — records audio, uses `/api/stt/transcribe` (WAV mono 16kHz)

Common options:
- `pushToTalk: true` (press-and-hold)
- `vad: true`, `vadThreshold`, `vadSilenceMs` (hub provider only)
- `autoSend: true` (send without confirmation)
