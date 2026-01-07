# SDK IO

SDK module: `adaos.sdk.io`

## Output (unified)

These helpers publish events onto the local bus. They do not write to Yjs directly.

- `io.out.chat.append(text, from_='hub', _meta={...})`
  - RouterService projects into `data.voice_chat.messages` of the target webspace.
- `io.out.say(text, lang='ru-RU', _meta={...})`
  - RouterService projects into `data.tts.queue` of the target webspace.

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
