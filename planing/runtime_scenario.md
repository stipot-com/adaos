1. Создать `src/adaos/sdk/scenarios/runtime.py` с типизированными моделями шага/сценария и исполнителем, опирающимся на SDK-порты (`adaos.sdk.data.memory`, будущие IO-адаптеры) вместо `services`.
2. Перенести регистрацию действий (IO, профиль, вызов навыков) в SDK-слой; заменить `_compose_greeting` на декларативные шаги/шаблоны внутри YAML.
3. Обновить `.adaos/scenarios/greet_on_boot/scenario.yaml`, чтобы он ссылался на новые SDK-маршруты и не обращался к `runtime.compose_greeting`.
4. Удалить/упростить `src/adaos/services/scenario_runner_min.py`, `ports_registry_min.py`, `skills_loader_fs.py`, перенаправив существующие вызовы на новый SDK API. Не вводить новые типы, если они уже введены в годе в другом месте. Например, src\adaos\services\scenario_runner_min.py secrets = _InMemorySecrets(store={}), но в src\adaos\ports\contracts.py есть class Secrets(Protocol):.
5. При необходмости расширить sdk, оставляя его тонким на services и adapters.
6. Обновить импорты в тестах/CLI на новый модуль.
1. Реализовать в SDK/сервисах вспомогательный модуль (например, `adaos/sdk/core/logging.py`), создающий `logging.Logger` с выводом в `{paths.logs_dir()}/scenarios/{id}.log`, используя подход `services/logging.py`.
2. Интегрировать логгер в исполнитель сценариев: логировать старт/завершение сценария и каждого шага, ошибки и вывод действий.
3. Обеспечить конфигурирование уровня логов через `Settings`/окружение и прокинуть путь лог-файла в bag результата.
1. Перенести проверки в `.adaos/scenarios/greet_on_boot/tests/test_scenario.py`, используя новый SDK runtime.
2. Добавить глобальный тестовый раннер `tests/test_scenarios.py`, который собирает `tests/` каталоги внутри `.adaos/scenarios/*/tests` (см. `_collect_test_dirs` в CLI `tests.py`).
3. Обновить документацию/README о запуске сценарных тестов.
4. Обновить существующие pytest-конфиги (например, `tests` CLI) для включения новых путей и окружения.
1. Добавить подкоманды `run`, `validate`, `test` в `src/adaos/apps/cli/commands/scenario.py`, вызывающие новый SDK runtime/валидатор и централизованный тестовый раннер.
2. Организовать вывод (успех, ошибки валидации, путь к логам) и обработку исключений по аналогии с командами навыков.
3. При необходимости обновить `src/adaos/apps/cli/commands/tests.py`, чтобы `adaos tests run --only-scenarios` (или флаг внутри существующей команды) запускал сценарные тесты через общий механизм.
1. В новом runtime реализовать действие `skills.run`/`skills.handle`, делегирующее в `adaos.services.skill.runtime.run_skill_handler_sync` или его SDK-обёртку, вместо локального загрузчика.
2. Удалить `skills_loader_fs.py`, перенеся нужные вспомогательные части (перехват stdout, контекст навыка) в SDK слой.
3. Обновить сценарий `greet_on_boot` на использование нового действия.
1. При необходимости обновить документацию в /docs/
