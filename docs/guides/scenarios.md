# Сценарии

Аналогично Skills:

- `{BASE_DIR}/workspace/scenarios` — рабочие директории сценариев.
- `{BASE_DIR}/scenarios` — read-only кэш монорепозитория.
- Таблицы `scenarios` и `scenario_versions`.

CLI:

```bash
# создание каркаса сценария по шаблону из монорепозитория
adaos scenario create demo --template template

# исполнение сценария и печать результатов
adaos scenario run greet_on_boot

# валидация структуры (проверка зарегистрированных действий)
adaos scenario validate greet_on_boot

# запуск тестов сценария (pytest из .adaos/workspace/scenarios/<id>/tests)
adaos scenario test greet_on_boot

# запустить тесты для всех сценариев
adaos scenario test
```

CLI использует URL монорепозитория, заданный в `SCENARIOS_MONOREPO_URL`, чтобы
поддерживать кэш `{BASE_DIR}/scenarios` и выдавать файлы в рабочие каталоги
`{BASE_DIR}/workspace/scenarios`.

## Модель данных (SQLite)

- Таблица `scenarios`:

  - `name` (PK), `active_version`, `repo_url`, `installed`, `last_updated`
- Таблица `scenario_versions` — как у навыков.

## Хранилище кода

- Путь: `{BASE_DIR}/scenarios` — **одно git-репо** (моно-репо сценариев, read-only кэш).
- Выборка подпапок через `git sparse-checkout` по БД.

## Сервис и CLI

Сервис: `services/scenario/manager.py` (аналогично `SkillManager`).

CLI подкоманды, связанные с управлением хранилищем и реестром, доступны в SDK
через `adaos.sdk.manage.scenarios.*`. Для локальных сценариев основной поток
работы идёт через команды `run`, `validate` и `test` выше.
