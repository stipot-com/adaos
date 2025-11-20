# Входящие события Telegram → Hub

Этот документ фиксирует контракт входящих сообщений, которые Hub получает из Root через NATS WS (или HTTP-фоллбэк). Это нужно для интеграции навыков/сценариев.

## Общая обёртка (Envelope)

- `type: "io.input"`
- `event_id: string` — уникальный ID события
- `ts: string` — ISO8601 UTC
- `dedup_key: string` — стабилизированный ключ для дедупликации
- `meta: { bot_id?: string, hub_id: string, trace_id: string, retries: number }`
- `payload: ChatInputEvent`

## ChatInputEvent

Общие поля:
- `type: "text" | "audio" | "photo" | "document" | "action" | "unknown"`
- `source: "telegram"`
- `bot_id: string`
- `hub_id: string` — целевой Hub (после адресации и/или резолва сессии)
- `chat_id: string`
- `user_id: string`
- `update_id: string`
- `payload: { ... }` — типоспецифичная структура (ниже)

### Text
```
payload: {
  text: string,
  meta: { msg_id: number, lang?: string }
}
```
- Если пользователь указал адресата `@<hub_id|alias> текст`, в поле `text` адресная часть отрезается (оставляется только текст), а `hub_id` — перенаправляется на указанный Hub.

### Photo
```
payload: {
  file_id: string,
  text?: string,               // подпись (caption), если была
  meta: { msg_id: number }
  file_path?: string           // добавляется на Hub: локальный путь к скачанному файлу
}
```
- На Hub при приёме файл скачивается в `get_ctx().paths.cache_dir()`; в событие добавляется `file_path`.
- Если caption начинается с `@<hub|alias> ...`, адресная часть удаляется, событие маршрутизируется на указанный Hub.

### Document
```
payload: {
  file_id: string,
  text?: string,               // подпись (caption), если была
  meta: { msg_id: number }
  file_path?: string           // добавляется на Hub
}
```

### Audio (voice)
```
payload: {
  file_id: string,
  meta: { msg_id: number, mime: "audio/ogg", duration?: number }
  file_path?: string           // добавляется на Hub
}
```

## Правила маршрутизации в Backend

- Без адресации (нет `@...`) — сообщение идёт в текущий Hub пользователя (сессия `/use`), при отсутствии — fallback к DEFAULT_HUB.
- С адресацией `@<hub_id|alias>` — сообщение идёт только в указанный Hub; префикс отрезается от текста/подписи.
- Управляющие команды (`/list`, `/help`, `/use`, `/current`, `/default`, `/alias`, `/bind_here`, `/unbind_here`) в Hub не отправляются (обрабатываются роутером Backend).
- Backend публикует в `tg.input.<hub>`; для обратной совместимости также дублирует в `io.tg.in.<hub>.text` (можно отключить на Hub).

## Примечания для навыков/сценариев

- Для медиа используйте `payload.file_path` — путь к локальной копии файла на Hub.
- Для текста используйте `payload.text` (адресный префикс уже удалён, если был).
- Язык пользователя, если присутствует, — в `payload.meta.lang`.

