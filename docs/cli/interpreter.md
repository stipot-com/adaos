# CLI: Интерпретатор и Rasa

## Общая схема

AdaOS поставляет встроенный пакет данных для интерпретатора (`src/adaos/interpreter_data/**`). При первом запуске CLI копирует эти файлы в профиль (`~/.adaos/state/interpreter/datasets`) и формирует рабочий проект для Rasa (`~/.adaos/state/interpreter/rasa_project`). Дополнительно любой установленный skill может описать собственные интенты в `skills/<имя>/interpreter/intents.yml` — генератор автоматически найдёт такие файлы и добавит примеры в датасет `skills_auto`.

Основная команда: `adaos interpreter train --engine rasa`. Она делает следующее:

1. Синхронизирует packaged пресет (`adaos interpreter dataset sync --preset moodbot` происходит автоматически).
2. Собирает YAML с интентами из `config.yaml` и `skills/*/interpreter/intents.yml`.
3. Разворачивает локальную виртуалку `~/.adaos/state/interpreter/.rasa-venv` (Python 3.10) и ставит `rasa==3.6.20`.
4. Запускает `rasa train nlu`, результат сохраняет в `~/.adaos/models/interpreter/interpreter_latest.tar.gz`.
5. Записывает отпечаток состояния в `~/.adaos/state/interpreter/metadata.json`.

## Интерпретатор CLI

```bash
adaos interpreter status                # проверка актуальности
adaos interpreter train --engine rasa   # обучение модели
adaos interpreter intent list|add|remove
adaos interpreter dataset list|sync
adaos interpreter init-skill NAME       # scaffold диспетчерского skill'а
```

`status` показывает количество интентов, файлов датасета, хэш состояния и причины, почему требуется обучение. Если config/dataset/skills_auto меняются, `needs_training` становится true.

## Описание интентов в skill'ах

Любой skill может дополнять интерпретатор, добавив файл `interpreter/intents.yml`. Формат:

```yaml
intents:
  - intent: my_skill.greet
    description: "Приветствие для моего скилла"
    skill: my_skill
    tool: on_start
    examples:
      - "привет мой скилл"
      - "покажи my_skill"
```

Во время `adaos interpreter train` генератор прочитает YAML из всех установленных skill'ов, создаст `datasets/skills_auto/nlu.yml` и включит их в обучение. Никаких ручных шагов не нужно — достаточно иметь `interpreter/intents.yml`.

## Packaged датасет

- `src/adaos/interpreter_data/moodbot/**` — базовый набор YAML (config/domain/nlu/stories/rules).
- `src/adaos/interpreter_data/config.default.yml` — шаблон `config.yaml`, где указан `dataset.preset: moodbot` и примеры интентов для диспетчерского skill'а.
- CLI хранит копию в `~/.adaos/state/interpreter/datasets/moodbot`.

При необходимости можно добавить свой пресет: поместите каталог в `src/adaos/interpreter_data/<название>`, после чего `adaos interpreter dataset list` покажет его, а `dataset sync --preset <название>` скопирует в профиль.

## Ручное тестирование модели

После `adaos interpreter train` модель лежит в `~/.adaos/models/interpreter/interpreter_latest.tar.gz`. Можно запустить Rasa shell:

```bash
cd ~/.adaos/state/interpreter
.rasa-venv/Scripts/rasa.exe shell --model ~/.adaos/models/interpreter/interpreter_latest.tar.gz
```

В интерактивном режиме видно JSON результата (`text`, `intent`, `intent_ranking`). Это полезно, чтобы убедиться, что интенты из skill'ов действительно подхватываются.

## Где искать состояния

- `~/.adaos/state/interpreter/config.yaml` — конфиг интентов (обновляется CLI).
- `~/.adaos/state/interpreter/datasets/**` — синхронизированные пресеты + `skills_auto`.
- `~/.adaos/state/interpreter/rasa_project` — рабочий проект, который `rasa train` использует как источник.
- `~/.adaos/state/interpreter/.rasa-venv` — изолированная среда Python 3.10 с Rasa.
- `~/.adaos/state/interpreter/metadata.json` — дата последнего обучения и отпечатки (config/datasets/skills_auto).
- `~/.adaos/models/interpreter/interpreter_latest.tar.gz` — последняя обученная модель.

Таким образом, другой разработчик после `git pull` и `pip install -e .[dev]` просто запускает `adaos interpreter train` и получает готовую Rasa-модель, построенную из packaged данных и установленных скиллов. Новые интенты добавляются автоматически через YAML в skill'ах.
