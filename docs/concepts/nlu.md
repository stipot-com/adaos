# NLU в AdaOS (prod MVP)

Цель: системный NLU на уровне AdaOS, который принимает текст (voice/telegram/web), определяет интент/слоты и мапит это в действия активного сценария. UI и навыки остаются декларативными и событийными.

## 1) Базовый pipeline (сегодня)

1. **Источник текста** публикует событие `voice.chat.user` (или другой input-event).
2. **Router**:
   - зеркалит сообщение пользователя в историю чата (YJS `data.voice_chat.messages`);
   - публикует `nlp.intent.detect.request` (команда “распознай интент”), обязательно с `_meta.route_id` (например `voice_chat`).
3. **NLU pipeline** (`src/adaos/services/nlu/pipeline.py`):
   - быстрый `regex`-этап для простых команд (MVP — “погода/weather” + `slots.city`);
   - если не распознано — публикует `nlp.intent.detect.rasa` (внутренний этап).
4. **Rasa bridge** (`src/adaos/services/nlu/rasa_service_bridge.py`):
   - вызывает Rasa-сервис;
   - публикует `nlp.intent.detected` при успехе;
   - публикует `nlp.intent.not_obtained` при ошибке/таймауте/невозможности распарсить.
5. **NLU dispatcher** (`src/adaos/services/nlu/dispatcher.py`):
   - читает `nlp.intent.detected`;
   - загружает `scenario.json["nlu"]` активного сценария;
   - выполняет описанные там actions (как bus events, типа `callSkill`/`callHost`).

## 2) Контракты событий (MVP)

### `nlp.intent.detect.request` (command)
Минимальный payload:
- `text: string`
- `webspace_id: string`
- `request_id: string` (для дедупликации/трейсинга)
- `_meta: { webspace_id, route_id, device_id?, trace_id?, scenario_id? }`

### `nlp.intent.detected` (event)
Минимальный payload:
- `intent: string`
- `confidence?: number`
- `slots: object`
- `text: string`
- `webspace_id: string`
- `request_id: string`
- `via: "regex" | "rasa" | ...`
- `_meta: {...}` (наследуется из request, чтобы ответы попадали в правильный route)

### `nlp.intent.not_obtained` (event)
Минимальный payload:
- `reason: string`
- `text: string`
- `webspace_id?: string`
- `request_id?: string`
- `via?: "rasa" | "dispatcher" | ...`
- `_meta?: {...}`

## 3) Сценарий как NLU-контекст

Сценарий — основная рамка контекста:
- активный сценарий определяет доступные интенты и их действия (`scenario.json["nlu"]`);
- переключение сценария = переключение NLU-контекста (в будущем: разные политики, разные “учителя”, разные подсказки/ограничения).

## 4) Ответы и UI-agnostic навыки

Навыки не должны знать о “чате” или “TTS”. Рекомендуемая модель:
- навыки публикуют типизированный результат, например `ui.notify { text, _meta }`;
- router решает, куда доставить (voice_chat, telegram, stdout) на основе `_meta.route_id` и routing-данных.

## 5) Отладка/наблюдаемость (YJS trace)

Для диагностики NLU-цепочки ведём лог в YJS:
- `data.nlu_trace.items` — последние N записей по событиям:
  - `nlp.intent.detect.request`
  - `nlp.intent.detected`
  - `nlp.intent.not_obtained`

Реализация: `src/adaos/services/nlu/trace_store.py` (подписки + запись в YDoc).

## 6) Later

Поверх текущего контракта можно подключать:
- retriever-NLU,
- LLM teacher-in-the-loop (на `nlp.intent.not_obtained`),
- Rhasspy/offline движки,
не меняя UI/DSL и не внедряя доменную логику во фронт.

