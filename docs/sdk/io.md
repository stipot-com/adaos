# SDK IO

SDK module: `adaos.sdk.io`

## Output (unified)

These helpers publish events onto the local bus. They do not write to Yjs directly.

- `io.out.chat.append(text, from_='hub', _meta={'webspace_id': '...'})`
  - RouterService projects into `data.voice_chat.messages` of the target webspace.
- `io.out.say(text, lang='ru-RU', _meta={'webspace_id': '...'})`
  - RouterService projects into `data.tts.queue` of the target webspace.

### Routeless skills via `_meta` context

When a tool is invoked with a payload that contains `_meta`, AdaOS automatically
sets an execution context so `io.out.*` helpers inherit it.

This means a skill can stay stateless and "routeless":

- no direct Yjs writes
- no explicit `_meta=...` in `chat_append()` / `say()` (unless you want to override)

RouterService also supports broadcasting by setting `_meta.webspace_ids = ['w1', 'w2', ...]`.

## Voice (local mock)

- `io.voice.stt.listen(timeout='20s')`
- `io.voice.tts.speak(text)`
