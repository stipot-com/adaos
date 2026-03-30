# CLI

Команда `adaos` - основная точка входа для оператора и разработчика.

## Основные группы команд

- `api`: запуск, остановка и перезапуск локального FastAPI control server
- `skill`: install, validate, run, update, activate и supervision навыков
- `scenario`: install, validate, run, test и compare сценариев
- `node`: статус роли, readiness, reliability, Yjs webspace и member-операции
- `hub`: join code и hub-root диагностика
- `autostart`: OS startup integration и действия core-update
- `monitor`: event и SSE-monitoring
- `runtime`: локальное runtime-состояние
- `secret`: хранение и чтение секретов
- `dev`: Root и Forge-style developer workflow
- `interpreter`: NLU datasets, parsing и training

## Частые примеры

```bash
adaos api serve
adaos skill list
adaos skill validate weather_skill
adaos scenario list
adaos node status --json
adaos hub join-code create
adaos autostart status
```

## Модель выполнения

Многие команды поддерживают два пути:

- прямое локальное исполнение через сервисы
- API-backed исполнение через активный control server

Это особенно заметно в командах для навыков, узла и сервисов.
