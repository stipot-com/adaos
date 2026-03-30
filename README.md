# AdaOS

![AdaOS CI](https://github.com/stipot-com/adaos/actions/workflows/ci.yml/badge.svg)

[Документация](https://stipot-com.github.io/adaos/)

## Установка

```bash
git clone -b rev2026 https://github.com/stipot-com/adaos.git
cd adaos

# 1. mac/linux:
sudo apt-get install -y python3.11-venv
bash tools/bootstrap.sh
source ./.venv/bin/activate
adaos --help

# 2. windows (PowerShell):
# 2.1. using astrav uv https://github.com/astral-sh/uv
powershell -ExecutionPolicy Bypass -File tools/bootstrap_uv.ps1
./.venv/Scripts/Activate.ps1
adaos --help

# 2.2. using pip
powershell -ExecutionPolicy Bypass -File ./tools/bootstrap.ps1
./.venv/Scripts/Activate.ps1
adaos --help


# При необходимости обновления пакетов uv
uv lock; uv sync

```

## Установка одной командой (init скрипты)

Если фронтенд хостит init-скрипты, можно “скачал и запустил”.

### Linux

```bash
curl -fsSL https://app.inimatic.com/assets/linux/init.sh | bash -s -- --join-code CODE
```

### Windows (PowerShell)

```powershell
iwr -UseBasicParsing https://app.inimatic.com/assets/windows/init.ps1 | iex; init.ps1 -JoinCode CODE
```

### Windows (CMD / .bat)

```bat
curl -fsSL -o init.bat https://app.inimatic.com/assets/windows/init.bat && init.bat -JoinCode CODE
```

Параметры после `--`/в конце команды прокидываются в `tools/bootstrap.*` (например `--role hub`, `--install-service auto`).

## Управление сервисом

```bash
# Перезапустить юнит
systemctl --user daemon-reload
systemctl --user restart adaos.service
# Проверить
systemctl --user status adaos.service --no-pager
journalctl --user -u adaos.service -n 120 --no-pager
curl http://127.0.0.1:8777/api/node/status (с X-AdaOS-Token, если требуется)
# Включить
adaos autostart enable
# Проверить
adaos autostart status
# Остановить
adaos autostart disable


adaos autostart update-status
adaos autostart update-start
# Контроль
cat ~/adaos/.adaos/state/core_update/status.json
cat ~/.adaos/state/core_update/status.json
adaos autostart update-cancel
adaos autostart update-rollback
adaos autostart smoke-update

# Практически на Linux теперь можно так:
adaos autostart update-status --json
adaos autostart smoke-update --countdown-sec 5 --json
adaos autostart update-cancel --json
adaos autostart update-rollback --json

# Рекомендованный smoke-порядок:
adaos autostart update-status --json
adaos autostart smoke-update --countdown-sec 30 --json
adaos autostart update-cancel --json
adaos autostart smoke-update --countdown-sec 5 --json


adaos node reliability
adaos node status
adaos node status --probe
adaos hub root watch
adaos hub root reconnect
adaos hub root reconnect --transport ws|tcp

# Boot debug 
# https://app.inimatic.com/?boot_debug=1

```

## Add a member node (phase 1)

1) On the hub node (role=hub), generate a short one-time code:

```bash
python -m adaos hub join-code create
```

1) On the member node, run bootstrap with that code (no tokens in CLI args). By default this joins via Root (`https://api.inimatic.com`):

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1 -JoinCode <CODE>
```

1) Verify local readiness:

```bash
python -m adaos node status --control http://127.0.0.1:8777 --json
```

Offline/LAN-only: create a local code on the hub with `python -m adaos hub join-code create --local` and run bootstrap with `-RootUrl http://<HUB_HOST>:8777` (Hub join entrypoint).

## В ситуации ModuleNotFoundError: No module named 'adaos'

1) Остановить все запущенные adaos, чтобы не было WinError 32
Get-Process adaos -ErrorAction SilentlyContinue | Stop-Process -Force

2) Удалить мусор после неудачной установки (часто ~daos-*.dist-info)
Remove-Item -Recurse -Force .\.venv\Lib\site-packages\~daos-*.dist-info -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force .\.venv\Lib\site-packages\adaos-*.dist-info -ErrorAction SilentlyContinue

3) Переустановить (пересоздаст adaos.exe)
.\.venv\Scripts\python.exe -m pip install -e .[dev]

## Обзор SDK

- **Публичные точки входа.** Высокоуровневые фасады доступны через `adaos.sdk.manage` (инструменты управления), `adaos.sdk.data` (датаплейн утилиты) и функцию `adaos.sdk.validate_self` (валидация текущего навыка).
- **Модель способностей.** Каждый инструмент проверяет разрешения через `ctx.caps`; названия способностей совпадают с именами инструментов (например, `manage.self`, `skills.manage`, `scenarios.manage`, `resources.manage`). При отсутствии прав возбуждается `adaos.sdk.core.CapabilityError`.
- **Контракт идемпотентности.** Все операции, изменяющие состояние, принимают параметры `request_id` и `dry_run`. Первый запуск фиксирует результат в KV-хранилище, повторный с тем же `request_id` возвращает сохранённый ответ без повторного исполнения.
- **Модель ошибок.** При отсутствии инициализированного контекста поднимается `SdkRuntimeNotInitialized`; превышение квот отображается как `QuotaExceeded`; конфликты состояния — через `ConflictError`.
- **Метаданные инструментов.** Декоратор `@tool` из `adaos.sdk.decorators` прикрепляет к каждой функции JSON-схемы входа/выхода и вспомогательные сведения (summary, stability). Экспортер `adaos.sdk.exporter.export` агрегирует эти описания для систем LLM.

## Использование

```bash
adaos --help
adaos skill install weather_skill
adaos skill list
adaos skill run weather_skill
adaos skill run weather_skill --topic nlp.intent.weather.get --payload '{"city": "Berlin"}'
adaos api serve --host 127.0.0.1 --port 8777
curl -i http://127.0.0.1:8777/health/live
curl -i http://127.0.0.1:8777/health/ready
# Windows запустить альтернативную ноду в той-же кодовой базе
$env:ADAOS_BASE_DIR_SUFFIX="_1"; adaos api serve --host 127.0.0.1 --port 8778

# Мониотринг
adaos api serve --host 127.0.0.1 --port 8777
$env:ADAOS_BASE_DIR_SUFFIX="_1"; adaos api serve --host 127.0.0.1 --port 8778
adaos monitor sse http://127.0.0.1:8777/api/observe/stream
curl -H "X-AdaOS-Token: dev-local-token" -X POST http://127.0.0.1:8778/api/observe/test
# Вариант 2
# 1) Узнай node_id member-ноды:
curl -H "X-AdaOS-Token: dev-local-token" http://127.0.0.1:8777/api/subnet/nodes
# 2) Подними SSE со строгим фильтром:
adaos monitor sse http://127.0.0.1:8777/api/observe/stream --topic net.subnet.
# 3) Попроси хаб дерегистрировать эту ноду (сразу прилетит net.subnet.node.down):
curl -H "X-AdaOS-Token: dev-local-token" -H "Content-Type: application/json" \
     -X POST http://127.0.0.1:8777/api/subnet/deregister \
     -d '{"node_id":"<member-node-id>"}'
# С хвостом
adaos monitor sse http://127.0.0.1:8777/api/observe/stream?replay_lines=50 --topic net.subnet.
```

```python
from adaos.services.skill.runtime import run_skill_handler_sync

print(
    run_skill_handler_sync(
        "weather_skill",
        "nlp.intent.weather.get",
        {"city": "Berlin"},
    )
)
```

### Сменить роль ноды

```python
headers = {"X-AdaOS-Token": "dev-local-token"}

# 1) проверить статус
print(requests.get("http://127.0.0.1:8778/api/node/status", headers=headers).json())

# 2) сменить роль на member или hub
payload = {"role": "member"}  # hub_url is deprecated/ignored; join-code flow sets hub_url in node.yaml
print(requests.post("http://127.0.0.1:8778/api/node/role", json=payload, headers=headers).json())

# 3) снова статус — должен быть role=member, ready=true
print(requests.get("http://127.0.0.1:8778/api/node/status", headers=headers).json())
```

## Использование

## AdaOS API

AdaOS CLI дополнен встроенным HTTP API (по умолчанию на `http://127.0.0.1:8777`).  
Аутентификация — через заголовок `X-AdaOS-Token`. Токен задаётся переменной окружения `ADAOS_API_TOKEN` (по умолчанию: `dev-local-token`).

### POST `/api/say`

Озвучивание текста через выбранный TTS-бэкенд (native / OVOS / Rhasspy).

**Запрос:**

```json
{
  "text": "Hello from AdaOS",
  "provider": "auto",     // необязательный параметр ("auto"|"ovos"|"rhasspy")
  "voice": "default"      // необязательный параметр (в зависимости от бэкенда)
}
````

**Ответ:**

```json
{
  "status": "ok",
  "provider": "ovos",
  "text": "Hello from AdaOS"
}
```

### Примеры использования

#### Linux / macOS

```bash
curl -X POST http://127.0.0.1:8777/api/say \
  -H "X-AdaOS-Token: dev-local-token" \
  -H "Content-Type: application/json" \
  -d '{"text":"Hello from AdaOS"}'
```

#### Windows PowerShell

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8777/api/say `
  -Headers @{ "X-AdaOS-Token" = "dev-local-token" } `
  -ContentType "application/json" `
  -Body (@{ text = "Hello from AdaOS" } | ConvertTo-Json)
```

---

### Статус / расширение API

- [x] `/api/say` — озвучивание текста
- [ ] `/api/listen` — захват аудио и STT (планируется)
- [ ] `/api/skills` — управление навыками
- [ ] `/api/runtime` — управление окружением

## **CLI AdaOS**

AdaOS CLI позволяет управлять навыками, тестами и Runtime через удобный интерфейс командной строки.
Все команды поддерживают локализацию (`ru`/`en`).

## **Общая структура**

```bash
adaos [OPTIONS] COMMAND [ARGS]...
```

- `OPTIONS` – глобальные опции CLI (например, `--help`).
- `COMMAND` – раздел команд: `skill`, `runtime`, `test`, `db`.
- `ARGS` – аргументы для конкретной команды.

---

## **Команды для работы с навыками**

```bash
adaos skill [SUBCOMMAND]
```

> Управление навыками: установка, удаление, обновление и отправка изменений в монорепозиторий.

---

### **1. Список установленных навыков**

```bash
adaos skill list
```

**Описание:**
Показывает все навыки, установленные у пользователя.

**Пример:**

```bash
adaos skill list
```

Вывод:

```
- AlarmSkill (активная версия: 1.0)
- WeatherSkill (активная версия: 0.3.2)
```

---

### **2. Создание нового навыка**

```bash
adaos skill create <skill_name> [--template <template_name>]
```

**Параметры:**

- `<skill_name>` – имя нового навыка.
- `--template, -t` – шаблон для инициализации навыка (по умолчанию `basic`).

**Пример:**

```bash
adaos skill create alarm_skill -t AlarmSkill
```

### **2. Проверка навыка**

Статические проверки (без импорта кода):

skill.yaml существует и валиден по схеме.

обязательные файлы: handlers/main.py.

dependencies: корректные строки; requirements.txt при желании.

tools[].name уникальны, схемы валидны (draft 2020-12).

events.subscribe[]/publish[] — строки, без дубликатов.

Динамические проверки (с импорта handler):
6) @tool("<name>") реально экспортирован для каждого tools[].name.
7) Есть подписчик @subscribe(topic) для каждого events.subscribe[].
8) (опц.) «сухой вызов» каждого инструмента с пустыми/моком аргументов — только если --probe-tools, иначе пропускаем.

```bash
adaos skill validate weather_skill
adaos skill validate weather_skill --json
adaos skill validate weather_skill --strict
```

---

### **3. Установка навыка из monorepo**

```bash
adaos skill install <skill_name>
```

**Описание:**
Добавляет навык из монорепозитория и помечает его как `installed=1`.
Обновляет `sparse-checkout`, чтобы навык появился в рабочей директории.

**Пример:**

```bash
adaos skill install weather_skill
```

---

### **4. Удаление навыка у пользователя**

```bash
adaos skill uninstall <skill_name>
```

**Описание:**
Удаляет навык из рабочей директории пользователя (флаг `installed=0`) и пересобирает `sparse-checkout`.

**Пример:**

```bash
adaos skill uninstall alarm_skill
```

---

### **5. Обновление навыка**

```bash
adaos skill update <skill_name>
```

**Описание:**
Подтягивает последнюю версию навыка из монорепозитория и обновляет версию в БД.

**Пример:**

```bash
adaos skill update weather_skill
```

---

### **6. Отправка изменений в монорепозиторий**

```bash
adaos skill push <skill_name> [-m <message>]
```

**Параметры:**

- `<skill_name>` – имя навыка.
- `-m, --message` – комментарий к коммиту (по умолчанию `Обновление навыка`).

**Пример:**

```bash
adaos skill push alarm_skill -m "Исправлены ошибки логики будильника"
```

---

### **7. Загрузка навыка (pull)**

```bash
adaos skill pull <skill_name>
```

**Описание:**
Принудительно загружает навык из монорепозитория. Используется при первой установке или восстановлении навыка.

---

### **8. Вывод активной версии навыка**

```bash
adaos skill versions <skill_name>
```

**Описание:**
Показывает текущую активную версию навыка.

**Пример:**

```bash
adaos skill versions alarm_skill
```

Вывод:

```
alarm_skill — активная версия: 1.0.0
```

---

## **Работа с Runtime, тестами и БД**

> В разработке.
> Команды `adaos runtime`, `adaos test` и `adaos db` будут добавлены в следующих версиях.

---

## **Примеры использования**

### Установка и настройка навыка из шаблона

```bash
adaos skill create weather_skill -t AlarmSkill
adaos skill push weather_skill -m "Добавлен навык прогноза погоды"
```

### Обновление навыков на последнюю версию

```bash
adaos skill list
adaos skill update alarm_skill
adaos skill update weather_skill
```

## Структура директорий локальной версии AdaOS (MVP)

project_root/
├── .env                     # Конфигурация окружения (SKILLS_REPO_URL и т.д.)
├── docker-compose.yml
├── requirements.txt
├── setup.py
├── README.md
│
├── src/adaos/
    ├── agent/                  # Runtime
    │   ├── core/              # State machine, skill_loader
    │   ├── db/                # SQLite persistence
    │   ├── i18n/              # TTS / UI strings
    │   └── audio/             # asr, tts, wake_word (если используется)
    │
    ├── sdk/                   # CLI и DevTools
    │   ├── cli/               # Typer CLI
    │   ├── skills/            # Компиляторы / шаблоны / генерация
    │   ├── llm/               # Подключение и промпты
    │   ├── locales/           # Локализация CLI
    │   └── utils/             # Общие утилиты (git, env)
    │
    ├── common/                # Опционально: конфиг, логгер, схемы
    └── skills_templates/      # YAML/intent-шаблоны
│
└── .adaos/                  # Рабочая среда пользователя (динамическая)
    ├── workspace/           # Редактируемые копии
    │   ├── skills/          # Локальные исходники навыков
    │   └── scenarios/       # Локальные исходники сценариев
    ├── skills/              # Кэш реестра навыков (read-only sparse checkout)
    ├── scenarios/           # Кэш реестра сценариев (read-only sparse checkout)
    ├── skill_db.sqlite      # База данных навыков (версии, установленные навыки)
    ├── models/              # Локальные модели (ASR и др.)
    │   └── vosk-model-small-ru-0.22/
    ├── runtime/             # Логи и тесты
    │   ├── logs/
    │   └── tests/

### Root / Forge интеграция

Сервер Root принимает обращения от CLI через REST API. По умолчанию используются dev-настройки:

- `ROOT_TOKEN` — токен для `POST /v1/*` (значение по умолчанию: `dev-root-token`).
- `ROOT_BASE` (`root_base` в `~/.adaos/root-cli.json`) — базовый URL Root (`http://127.0.0.1:3030`).
- `ADAOS_BASE` и `ADAOS_TOKEN` — адрес и токен локального AdaOS моста, передаются в заголовках при `GET /adaos/healthz`.

CLI автоматически выполняет preflight-проверки, регистрирует subnet/node и отправляет архив черновика:

```bash
adaos skill install weather_skill
adaos skill create demo --template python-minimal
adaos skill push demo

adaos scenario create morning
adaos scenario push morning
```

Для сброса кэша реестра используйте `adaos repo reset skills` или `adaos repo reset scenarios` — команда выполнит `git fetch`/`reset --hard`/`clean -fdx` внутри `.adaos/skills` или `.adaos/scenarios`.

## **Схема локальной версии (PlantUML)**

```plantuml
@startuml Local_AdaOS_MVP
!includeurl https://raw.githubusercontent.com/plantuml-stdlib/C4-PlantUML/master/C4_Container.puml

Container(user, "Пользователь", "Голос или текст", "Формулирует запросы")
Container(llm, "LLM Client", "OpenAI / litellm", "Генерация тестов и навыков по запросу")
Container(testrunner, "Test Runner", "PyYAML + pytest", "Прогоняет тесты BDD для навыков")
Container(git, "Git Repo", "GitPython", "Версионирование навыков")
Container(sqlite, "Skill DB", "SQLite + SQLAlchemy", "Метаданные навыков и версий")
Container(runtime, "Skill Runtime", "importlib + watchdog", "Запуск навыков и проверка прав")
Container(logs, "Логирование", "logging + rich", "Хранение логов и ошибок")

Rel(user, llm, "Говорит/пишет запрос")
Rel(llm, testrunner, "Создает тест")
Rel(testrunner, runtime, "Проверяет в песочнице")
Rel(testrunner, git, "Сохраняет успешный навык")
Rel(testrunner, sqlite, "Записывает метаданные")
Rel(runtime, sqlite, "Читает активные версии навыков")
Rel(runtime, logs, "Пишет логи работы и ошибок")

@enduml
```

## **Как работают библиотеки и сервисы**

1. **LLM** (OpenAI / Ollama через `openai` или `litellm`)

   - Генерирует тест (YAML) и код навыка.

2. **TestRunner** (`PyYAML`, `pytest`)

   - Прогоняет тест на существующих навыках.
   - Если провал – генерирует и тестирует новый навык в песочнице.

3. **GitPython**

   - Хранит каждую версию навыка в `skills_repo/`.
   - Теги: `AlarmSkill_v1.0`.

4. **SQLite + SQLAlchemy**

   - Записывает: версия, путь к активной директории навыка, дата создания.

5. **Runtime** (`importlib`, `watchdog`)

   - Подхватывает активные навыки из `skills/active/`.
   - Обрабатывает intent → вызывает handler.py → проверяет права.

6. **Логирование** (`logging + rich`)

   - Все ошибки тестов и Runtime пишутся в `runtime.log`.
   - Возможна интеграция с CLI для просмотра.

## Skill

### **1. Основной принцип: навык = код + манифест (SDK)**

- **Каждый навык — это модуль на Python**: `manifest.yaml + handler.py`.
- **SDK максимально минималистичный** (10–15 функций) и похож на известные фреймворки (Flask, FastAPI, Alexa Skills Kit).
- Навык выполняется в общем рантайме без тяжёлого sandbox, но с **системой прав как в Android**.

Пример:

```yaml
# manifest.yaml
name: AlarmSkill
version: 1.0
permissions:
  - audio.playback
  - time.schedule
intents:
  - set_alarm
  - cancel_alarm
```

```python
# handler.py
from skill_sdk import speak, set_alarm, cancel_alarm

def handle(intent, entities):
    if intent == "set_alarm":
        set_alarm(entities["time"])
        speak("Будильник установлен")
    else:
        cancel_alarm()
        speak("Будильник отменён")
```

### **2. Система прав вместо глубокой изоляции**

- Навык при установке получает **фиксированный набор прав** на ресурсы (микрофон, TTS, сетевые вызовы и т.д.).
- Магазин проверяет права и код (статический анализ).
- В случае критической ошибки возможен быстрый откат навыка через CI/CD.

### **3. Генерация навыков через LLM и визуальный UI**

- **LLM выступает главным создателем навыков**, получая только компактную документацию по SDK и несколько примеров.
- Для пользователей без навыков программирования на сервере AdaOS делаем **UI-конструктор**:

  - Пользователь описывает навык голосом или текстом.
  - LLM генерирует код и манифест.
  - Код проверяется и устанавливается через магазин.

Таким образом:

- LLM генерирует 90% навыков без человека.
- Разработчики могут писать сложные навыки руками.

---

### **4. CI/CD и магазин навыков как основа безопасности**

- Все навыки проходят через **автоматизированный пайплайн**:

  1. Проверка прав и зависимостей.
  2. Прогон в тестовом окружении / эмуляторе.
  3. Подпись и публикация в магазине.

- Магазин управляет версиями SDK и навыков, как App Store.

---

### **5. Лёгкая возможность иерархии и переиспользования навыков**

- Навык может **вызвать другой навык** через SDK (`invoke_skill(skill_id, params)`).
- Это позволяет строить иерархию без сложного DSL-графа.
- Визуальный редактор на сервере может отображать эти связи как **граф**, но это всего лишь UI-надстройка.

### Skill Lifecycle

```plantuml
@startuml Skill_Lifecycle
!includeurl https://raw.githubusercontent.com/plantuml-stdlib/C4-PlantUML/master/C4_Component.puml

LAYOUT_WITH_LEGEND()

Container(skillMgr, "SkillManager", "Python", "Управляет жизненным циклом навыков")
Component(store, "Local Skill Store", "Filesystem", "Хранилище навыков")
Component(registry, "Skill Registry", "Git / AdaOS Server", "Централизованный каталог")
Component(executor, "SkillExecutor", "Python Sandbox", "Изолированное исполнение навыков")

Rel(skillMgr, registry, "Запрашивает метаинформацию / навык")
Rel(skillMgr, store, "Сохраняет/удаляет/обновляет")
Rel(skillMgr, executor, "Передаёт навык на запуск")
Rel(executor, store, "Читает код / настройки")
Rel(executor, skillMgr, "Сообщает результат")

@enduml

```

## Таблица взаимодействий с AdaOS Server

| Этап                    | Запрос от Reutilizer   | Ответ от AdaOS Server                    |
| ----------------------- | ---------------------- | ---------------------------------------- |
| 📡 Регистрация          | `POST /register` + ID  | `200 OK` + `config`, `skill_list`        |
| 🔁 Синхронизация        | `GET /skills/update`   | Список доступных обновлений              |
| ⬇️ Установка навыка     | `GET /skills/{id}`     | Архив с кодом + манифест                 |
| 📥 Отправка логов       | `POST /logs`           | `200 OK` или `retry`                     |
| 📤 Загрузка данных      | `POST /data` (sensors) | `200 OK` или `rules to trigger`          |
| 🔃 Обновление состояния | `PATCH /status`        | Могут быть переданы команды или сценарии |

### Общая архитектура

```plantuml
@startuml Hybrid_Approach
!includeurl https://raw.githubusercontent.com/plantuml-stdlib/C4-PlantUML/master/C4_Context.puml
!includeurl https://raw.githubusercontent.com/plantuml-stdlib/C4-PlantUML/master/C4_Container.puml

LAYOUT_WITH_LEGEND()

Person(user, "Пользователь", "Создаёт или использует навыки")

System_Boundary(s1, "AdaOS Server") {
  
  Container(ui, "Web UI / Voice UI", "React / Ionic", "Интерфейс для управления навыками и устройствами")
  Container(api, "API Gateway", "FastAPI / GraphQL", "Единая точка входа для UI и LLM")
  
  Container(store, "Skill Store", "PostgreSQL + S3", "Хранилище навыков, версий и прав доступа")
  Container(ci, "CI/CD Pipeline", "GitHub Actions / Drone CI", "Проверка и подписание навыков")
  
  Container(llm, "LLM Engine", "ChatGPT / Ollama", "Генерация и модификация навыков")
  
  Container(deviceReg, "Device Registry", "Redis + PostgreSQL", "Регистрация и статус устройств")
  Container(mqtt, "Messaging Broker", "MQTT / WebSocket", "Команды и обновления для устройств")

}

System_Boundary(s2, "Устройство с AdaOS") {
  Container(runtime, "Skill Runtime", "Python + Skill SDK", "Исполнение навыков с системой прав")
  Container(updater, "Updater", "Git / HTTPS", "Обновление навыков и ядра системы")
}

Rel(user, ui, "Управляет навыками и устройствами")
Rel(ui, api, "Вызывает")
Rel(api, llm, "Запрос на генерацию навыка")
Rel(api, store, "Чтение / запись навыков")
Rel(api, ci, "Инициирует проверку и подписание навыка")
Rel(api, deviceReg, "Обновляет статусы устройств")
Rel(api, mqtt, "Отправляет команды")
Rel(store, ci, "Отдаёт исходники для проверки")

Rel(mqtt, runtime, "Доставляет команды и обновления")
Rel(updater, store, "Скачивает обновлённые навыки")
Rel(runtime, store, "Устанавливает навыки через SDK")

@enduml

```

## **Примеры использования CLI**

### Создание навыка

### TODO

- [x] Сбор словарей из lang_res и генерация локализации
- [x] Перенос prep request в папку навыка
- [ ] Постобработка папки навыка для его улучшения. Например, можно обратить внимание, что логи не достаточно информативны и скорректировать prepare.py. Или регулярная ошибка может говорить о необходимости найти причины и отладиться.

```bash
# Создать навык weather_skill на основе шаблона AlarmSkill
adaos skill create weather_skill -t AlarmSkill
# Сформировать запрос для LLM на генерацию кода prepare.py подготовительной работы
adaos llm build-prep weather_skill "Научись узнавать погоду на сегодня"
# Запрос используем для генерации prepare.py
# Исполнение подготовительной работы prepare.py и сохранение логов
adaos skill prep weather_skill
# Сформировать запрос для LLM на генерации кода навыка
adaos llm build-skill weather_skill "Научись узнавать погоду на сегодня"
# Запрос используем для генерации handlers/main.py, skill.yaml
# Запустить навык
adaos skill run weather_skill get_weather
# Обновить навык из репозитория
adaos skill update weather_skill
```

### Список навыков

```bash
python cli.py skill list
```

### Версии навыка

```bash
python cli.py skill versions AlarmSkill
```

### Запуск теста вручную

```bash
python cli.py test run src/skills/AlarmSkill/tests/test_alarm.yaml
```

### Откат последнего коммита

```bash
python cli.py skill rollback
```

### Логи Runtime

```bash
python cli.py runtime logs
```

## Запуск локальной версии

```bash
# Сборка
docker-compose build

# Запуск
docker-compose up -d

# Войти внутрь контейнера и запустить CLI
docker exec -it adaos bash
python cli.py skill list

```

/build
/docs
/src
  /adaos
    /core/                 # чистые функции: планирование, валидаторы, преобразования
      /scenario_engine
    /domain/               # dataclass DTO/VO, типы событий, конфиги
    /ports/                # Protocol/ABC: GitClient, PathProvider, *Repository, Runtime, EventBus
    /adapters/             # реализации портов (весь I/O)
      /fs
      /db                  # sqlite и т.п.
      /git
      /audio
        /stt
        /tts
      /ovos
      /rhasspy
      /android
      /inimatic
    /services/             # объектные «оболочки» поверх core и портов
      /skill
      /scenario
      /runtime
      /orchestrator
    /apps/                 # исполняемые приложения (входные точки)
      /cli                 # Typer
      /api                 # FastAPI/Flask
      /launcher
        /linux
        /windows
    /sdk/                  # публичные типы/интерфейсы для внешних навыков/сценариев
      /llm
      /locales
      /skills
      /utils
      /abi                 # если это публичные форматы — сюда
    /templates/
      /skills
      /scenarios
/tests
/tools
