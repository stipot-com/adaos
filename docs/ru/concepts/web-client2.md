# Унификация веб интерфейса на данных

## Варианты использования

Рассмотрим первоначальные варианты использования

## 1. Медиацентр

### Что нужно конечному пользователю

* библиотека: фильмы / сериалы / музыка / плейлисты;
* удобный **поиск и фильтр**;
* «плиточная» галерея постеров/обложек;
* экран «сейчас играет» с управлением;
* история просмотров / рекомендательные списки.

### Паттерны

* библиотека → коллекции;
* проигрывание → спец-визуализация;
* история / очереди → списки/таймлайны.

### Нейтральные компоненты

**Коллекции:**

* `collection.cards` — каталог фильмов/альбомов как сетка карточек;
* `collection.grid` — более простая иконочная/постерная сетка (можем ввести как отдельный тип для «чистых плиток» без сложной карточки);
* `collection.list` — очереди воспроизведения, история.

**Ввод и фильтрация:**

* `input.searchBox` — поиск по названию;
* `input.filterBar` — жанр, год, источник (локально/онлайн);
* `input.commandBar` — кнопки «добавить к плейлисту», «в избранное», «запустить с начала» и т.п.

**Визуализация / спец-компоненты:**

* новый тип `visual.mediaPlayer` — зона «сейчас играет» с:

  * обложкой,
  * кнопками Play/Pause/Next,
  * ползунком прогресса и громкости.
* `visual.metricTile` — простые метрики: “Новых эпизодов: 5”, “В очереди: 12”.

**Оверлеи:**

* `overlay.modal` — подробная карточка фильма/альбома;
* `overlay.sideSheet` — плейлист/очередь.

> Вывод: для медиацентра нам *достаточно* существующих коллекций + **добавить один специализированный компонент** `visual.mediaPlayer` и, возможно, `collection.grid` как лёгкую плиточную сетку.

---

## 2. Список покупок

### Что нужно пользователю

* один или несколько списков (дом, дача, поездка);
* группировка по магазину/категории;
* быстрый ввод;
* отметка «куплено»;
* иногда — количество/единицы измерения.

### Паттерны

* простые коллекции, минимум визуальных наворотов.

### Нейтральные компоненты

* `collection.list` — список позиций;
* `collection.board` — если хотим разделить на колонки:

  * «Запланировано / В корзине / Куплено»;
* `item.form` — форма создания/редактирования позиции:

  * название, категория, количество;
* `input.commandBar` — быстрые действия: «добавить позицию», «очистить купленное»;
* `input.filterBar` — фильтр по списку/магазину/категории;
* `feedback.notification` — всплывашки «добавлено в список».

> Вывод: список покупок полностью укладывается в уже имеющийся базовый набор, **новых типов не требует**.

---

## 3. Календарь дел / планировщик

Здесь как раз всплывает важный отдельный паттерн — календарная сетка.

### Что нужно

* календарь (месяц/неделя/день);
* список задач/событий;
* drag&drop или быстрые изменения даты/времени;
* напоминания, отметка выполнения;
* привязка задач к проектам/контекстам.

### Паттерны

* дата-временной **grid**;
* таймлайн/agenda;
* формы задач.

### Нейтральные компоненты

**Коллекции и визуализация:**

* новый тип `visual.calendar`:

  * это компонент, который показывает:

    * сетку дней (месяц),
    * или шкалу времени (день/неделя),
    * и умеет поверх отрисовать события;
* `collection.timeline` — agenda-вид: «Список событий по времени»;
* `collection.list` / `collection.board` — список задач и KANBAN по статусу.

**Один элемент:**

* `item.form` — карточка события/задачи (название, время, напоминания, проект и т.п.);
* `item.details` — просмотр события.

**Ввод / фильтр:**

* `input.selector` — выбор календаря / проекта;
* `input.commandBar` — «создать событие», «перейти к сегодня», «показать неделю/месяц»;
* `input.filterBar` — фильтры по типу событий.

> Вывод: нужно явно добавить **один новый тип** `visual.calendar`, всё остальное уже есть.

---

## 4. Рабочий стол с приложениями

Это, по сути, **лаунчер** и/или **дашборд**.

### Что нужно

* сетка “иконок” приложений или рабочих мест;
* возможность закрепить избранные;
* иногда — виджеты (минитайлы с состоянием: «сегодня задач: 5»).

### Паттерны

* плитка/сеточная коллекция;
* метрики/мини-виджеты.

### Нейтральные компоненты

* `collection.grid` — сетка «иконок» (рабочие места, приложения, сценарии);
* `visual.metricTile` — маленькие виджеты по состоянию системы/профиля;
* `layout.tabs` — если хотим разделить рабочий стол на вкладки: «Работа», «Личное», «Системные»;
* `input.searchBox` — быстрый поиск по приложениям/сценариям.

> Вывод: здесь нам в самый раз пригодится `collection.grid`. Никаких новых видов, кроме уже упомянутых, не нужно.

---

## 5. Базовый набор нейтральных компонентов

Зафиксируем **ядро 1-й версии** так:

### layout.*

* `layout.page` — страница с заголовком и областями.
* `layout.split` — разбиение на области (sidebar/main/bottom).
* `layout.tabs` — вкладки.
* `layout.panel` — панель/карточка внутри области.

### collection.*

* `collection.list` — список.
* `collection.table` — таблица.
* `collection.cards` — карточки.
* `collection.grid` — равномерная плитка (иконки, постеры, мини-виджеты).
* `collection.tree` — дерево.
* `collection.board` — доска (колонки + карточки).
* `collection.timeline` — временная последовательность (шаги, события).

### item.*

* `item.preview` — компактный просмотр элемента.
* `item.details` — подробный просмотр (read-only).
* `item.form` — редактирование сущности через форму.
* `item.textEditor` — текстовый/markdown редактор.
* `item.codeEditor` — редактор кода/структурного текста.

### input.*

* `input.commandBar` — панель действий (кнопки/меню).
* `input.filterBar` — фильтры/чипсы/переключатели.
* `input.searchBox` — поиск.
* `input.selector` — селект/переключатель контекстов (workspace, календарь, источник).

### visual.*

* `visual.metricTile` — числовая/статусная плитка.
* `visual.chart` — базовый график.
* `visual.calendar` — календарь (месяц/неделя/день).
* `visual.mediaPlayer` — плеер «сейчас играет».

### feedback.*

* `feedback.log` — поток лог-сообщений/чат.
* `feedback.notification` — список уведомлений.
* `feedback.statusBar` — строка статуса.

### overlay.*

* `overlay.modal` — модальное окно.
* `overlay.sideSheet` — боковая панель.
* `overlay.confirm` — подтверждение.

Этого набора достаточно, чтобы:

* сделать **рабочее место промт-инженера**;
* собрать **медиацентр**;
* сделать **список покупок**;
* сделать **календарь дел**;
* организовать **рабочий стол с приложениями** — и ещё кучу вещей типа менеджера навыков, observability-панели и т.п.

## Фиксируем **DSL для UI** и на двух примерах

1. рабочее место промт-инженера,
2. медиацентр.

## 1. Базовый формат PageSchema

### 1.1. Типы (как ориентир для LLM-программиста)

```ts
// Типы компонентов (помним нашу нейтральную палитру)
type WidgetType =
  | 'collection.list'
  | 'collection.table'
  | 'collection.cards'
  | 'collection.grid'
  | 'collection.tree'
  | 'collection.board'
  | 'collection.timeline'
  | 'item.preview'
  | 'item.details'
  | 'item.form'
  | 'item.textEditor'
  | 'item.codeEditor'
  | 'input.commandBar'
  | 'input.filterBar'
  | 'input.searchBox'
  | 'input.selector'
  | 'visual.metricTile'
  | 'visual.chart'
  | 'visual.calendar'
  | 'visual.mediaPlayer'
  | 'feedback.log'
  | 'feedback.notification'
  | 'feedback.statusBar'
  | 'overlay.modal'
  | 'overlay.sideSheet'
  | 'overlay.confirm';
```

### 1.2. Структура страницы

**Концепция простая:**

* `layout.areas` — именованные зоны (slots) страницы.
* `widgets` — элементы, которые рендерятся в эти зоны.
* Всё, что касается данных и поведения — в `dataSource` и `actions`.

```ts
interface PageSchema {
  id: string;
  title?: string;
  layout: {
    type: 'single' | 'split' | 'custom';
    areas: Array<{
      id: string;         // уникальный ID зоны (например, "sidebar", "main", "right", "bottom")
      role?: string;      // семантика для LLM: "sidebar" | "main" | "aux" | "footer" и т.п.
    }>;
  };
  widgets: WidgetConfig[];
}

interface WidgetConfig {
  id: string;
  type: WidgetType;       // из словаря выше
  area: string;           // ссылка на layout.areas[id]
  title?: string;

  // Где брать данные
  dataSource?: DataSourceConfig;

  // Как интерпретировать данные и настраивать внешний вид
  inputs?: Record<string, any>;

  // Что делать по событиям (клики, сохранение, выбор итерации и т.п.)
  actions?: ActionConfig[];

  // Условия видимости
  visibleIf?: string;     // выражение типа "$state.currentIterationId != null"
}

interface DataSourceConfig {
  kind: 'skill' | 'api' | 'static';
  name?: string;          // для kind=skill: имя навыка/метода
  url?: string;           // для kind=api: URL
  method?: 'GET'|'POST';  // для kind=api
  params?: Record<string, any>;     // query / аргументы
  body?: any;                         // шаблон тела запроса
  // (опционально можно добавить mapping, но на MVP можно обойтись convention'ами)
}

interface ActionConfig {
  on: string;             // событие: "click", "select", "submit", "run", "save" и т.п.
  type: 'callSkill' | 'updateState' | 'navigate' | 'openOverlay';
  target?: string;        // имя навыка / id страницы / id overlay-схемы
  params?: Record<string, any>; // с шаблонами: "$event.id", "$state.currentNode" и т.п.
}
```

**Соглашение по ссылкам:**

* `$state.xxx` — данные общего состояния страницы (PageStateService).
* `$event.xxx` — данные из события (что-то выбрали/нажали).
* `$data.xxx` — если нужно сослаться на загруженные данные виджета (можно добавить позже).

---

## 2. Пример 1: Рабочее место промт-инженера (YAML)

Здесь **нет никаких слов "prompt" в типах**, домен только в `name` навыков и ключах.

```yaml
id: prompt-engineer-workspace
title: Рабочее место промт-инженера

layout:
  type: split
  areas:
    - id: sidebar
      role: sidebar
    - id: main
      role: main
    - id: right
      role: aux
    - id: bottom
      role: footer

widgets:
  # 1) Дерево проекта (сценарии, навыки, файлы)
  - id: project-tree
    type: collection.tree
    area: sidebar
    title: Проект
    dataSource:
      kind: skill
      name: prompt_workspace.loadProjectTree
      params:
        projectId: "$state.currentProjectId"
    inputs:
      labelField: label
      iconField: icon
      childrenField: children
    actions:
      - on: select
        type: updateState
        params:
          currentNodeRef: "$event.node.ref"

  # 2) Редактор содержимого узла (код/markdown/конфиг)
  - id: node-editor
    type: item.codeEditor
    area: main
    title: Редактор
    dataSource:
      kind: skill
      name: prompt_workspace.loadNodeContent
      params:
        nodeRef: "$state.currentNodeRef"
    inputs:
      languageFromPath: true      # язык определяем по расширению
      readOnlyIfNoNode: true
    actions:
      - on: save
        type: callSkill
        target: prompt_workspace.saveNodeContent
        params:
          nodeRef: "$state.currentNodeRef"
          content: "$event.content"

  # 3) Таймлайн итераций (слева список версий/итераций)
  - id: iteration-timeline
    type: collection.timeline
    area: right
    title: Итерации
    dataSource:
      kind: skill
      name: prompt_workspace.listIterations
      params:
        projectId: "$state.currentProjectId"
    inputs:
      order: desc
      timeField: createdAt
      titleField: title
      subtitleField: status
    actions:
      - on: select
        type: updateState
        params:
          currentIterationId: "$event.item.id"
      - on: create
        type: callSkill
        target: prompt_workspace.createIteration
        params:
          projectId: "$state.currentProjectId"

  # 4) Таймлайн шагов текущей итерации (внизу)
  - id: step-timeline
    type: collection.timeline
    area: bottom
    title: Шаги итерации
    dataSource:
      kind: skill
      name: prompt_workspace.listSteps
      params:
        iterationId: "$state.currentIterationId"
    inputs:
      order: asc
      timeField: createdAt
      titleField: title
      statusField: status
    actions:
      - on: select
        type: updateState
        params:
          currentStepId: "$event.item.id"
      - on: run
        type: callSkill
        target: prompt_workspace.runStep
        params:
          stepId: "$event.item.id"

  # 5) Логи запусков (LLM ответы, статусы шагов)
  - id: run-log
    type: feedback.log
    area: bottom
    title: Логи
    dataSource:
      kind: skill
      name: prompt_workspace.streamLogs
      params:
        iterationId: "$state.currentIterationId"
        stepId: "$state.currentStepId"
    inputs:
      levelField: level
      messageField: message
      timeField: timestamp
      sourceField: source

  # 6) Инспектор текущего контекста (шаг/итерация/узел)
  - id: inspector
    type: item.form
    area: right
    title: Инспектор
    dataSource:
      kind: skill
      name: prompt_workspace.inspectContext
      params:
        nodeRef: "$state.currentNodeRef"
        iterationId: "$state.currentIterationId"
        stepId: "$state.currentStepId"
    inputs:
      layout: auto
      readOnly: false
    actions:
      - on: submit
        type: callSkill
        target: prompt_workspace.updateContext
        params:
          values: "$event.values"

  # 7) Панель команд (кнопки вверху/снизу)
  - id: command-bar
    type: input.commandBar
    area: main
    title: Действия
    inputs:
      buttons:
        - id: run-current-step
          label: Запустить шаг
          icon: play
          enabledIf: "$state.currentStepId != null"
        - id: create-iteration
          label: Новая итерация
          icon: add
        - id: publish-iteration
          label: Опубликовать
          icon: rocket
          enabledIf: "$state.currentIterationId != null"
    actions:
      - on: click:run-current-step
        type: callSkill
        target: prompt_workspace.runStep
        params:
          stepId: "$state.currentStepId"
      - on: click:create-iteration
        type: callSkill
        target: prompt_workspace.createIteration
        params:
          projectId: "$state.currentProjectId"
      - on: click:publish-iteration
        type: callSkill
        target: prompt_workspace.publishIteration
        params:
          iterationId: "$state.currentIterationId"
```

Здесь:

* LLM видит только **нейтральные типы компонентов**;
* вся конкретика промт-инжиниринга — в `name` навыков и полях;
* одна и та же фронтовая сборка умеет рендерить это и любые другие схемы.

---

## 3. Пример 2: Медиацентр (YAML)

Теперь прогоняем ту же схему через **медиацентр**.

```yaml
id: media-center
title: Медиатека

layout:
  type: split
  areas:
    - id: sidebar
      role: sidebar
    - id: main
      role: main
    - id: bottom
      role: footer

widgets:
  # 1) Поиск и фильтры (жанр, тип, источник)
  - id: media-filter
    type: input.filterBar
    area: sidebar
    title: Фильтры
    inputs:
      fields:
        - id: query
          type: search
          label: Поиск
        - id: type
          type: select
          label: Тип
          options:
            - { value: "movie", label: "Фильмы" }
            - { value: "series", label: "Сериалы" }
            - { value: "music", label: "Музыка" }
        - id: source
          type: select
          label: Источник
          options:
            - { value: "local", label: "Локально" }
            - { value: "online", label: "Онлайн" }
    actions:
      - on: change
        type: updateState
        params:
          mediaFilter: "$event.values"

  # 2) Плитка контента (каталог)
  - id: media-grid
    type: collection.grid
    area: main
    title: Медиатека
    dataSource:
      kind: skill
      name: media_center.listItems
      params:
        filter: "$state.mediaFilter"
    inputs:
      imageField: posterUrl
      titleField: title
      subtitleField: year
      badgeField: rating
      columns: 5
    actions:
      - on: select
        type: updateState
        params:
          currentMediaId: "$event.item.id"
      - on: doubleSelect
        type: callSkill
        target: media_center.playNow
        params:
          mediaId: "$event.item.id"

  # 3) Плеер "сейчас играет"
  - id: now-playing
    type: visual.mediaPlayer
    area: bottom
    title: Сейчас играет
    dataSource:
      kind: skill
      name: media_center.getNowPlaying
      params: {}
    inputs:
      titleField: title
      artistField: artist
      durationField: duration
      positionField: position
      coverField: coverUrl
      progressEditable: true
    actions:
      - on: control:play
        type: callSkill
        target: media_center.play
        params: {}
      - on: control:pause
        type: callSkill
        target: media_center.pause
        params: {}
      - on: control:next
        type: callSkill
        target: media_center.next
        params: {}
      - on: control:seek
        type: callSkill
        target: media_center.seek
        params:
          position: "$event.position"

  # 4) Очередь воспроизведения
  - id: play-queue
    type: collection.list
    area: sidebar
    title: Очередь
    dataSource:
      kind: skill
      name: media_center.getQueue
      params: {}
    inputs:
      titleField: title
      subtitleField: info
    actions:
      - on: select
        type: callSkill
        target: media_center.playFromQueue
        params:
          mediaId: "$event.item.id"

  # 5) История просмотров/прослушиваний
  - id: history
    type: collection.timeline
    area: sidebar
    title: История
    dataSource:
      kind: skill
      name: media_center.getHistory
      params: {}
    inputs:
      timeField: finishedAt
      titleField: title
      subtitleField: info
```
