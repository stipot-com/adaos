# Greet_on_boot

минимальный, но «правильно собранный» runtime-сценарий с аудитом окружения, установкой имени, приветствием и погодой, с аккуратной деградацией каналов (tg → voice) и идемпотентностью.

## что делает сценарий

* триггер: `node.online` (и защита от повторов: не чаще 1 раза в сутки на ноду/пользователя).
* шаги: аудит окружения → выбор каналов вывода → проверка/запрос имени → приветствие → краткая сводка погоды → отправка в tg и озвучивание → телеметрия.
* правила деградации: если нет конфигурации Telegram — работаем только голосом; если нет TTS — только Telegram; если нет обоих — складываем в локальный инбокс.

## контракты портов (кратко)

* `system_audit.run() -> {os, cpu, mem, skills[], channels[], …}` — навык «ревизии».
* `profile.get_user() -> {id, name?}` / `profile.set_name(name)`.
* `io.telegram.send(text, parse_mode?, chat_id?)`.
* `io.voice.tts.speak(text)` и опционально `io.voice.stt.listen(timeout) -> {text?}`.
* `weather.brief(location?) -> {text}` — ваш существующий навык, возвращает уже готовый текст «сегодня …».
* `location.current() -> {city?, lat?, lon?}` — если погода умеет самодоставание локации, этот шаг можно опустить.

## YAML-спецификация сценария (v0, лаконичный DSL)

```yaml
id: greet_on_boot
version: 0.1
trigger:
  on: node.online
idempotency:
  key: "greet:${ctx.node.id}:${ctx.user.id}:${date.today}"
  ttl: "36h"
policy:
  run_window_local: "07:00-22:30"
  timeout: "30s"

vars:
  outputs_preferred: ["telegram", "voice"]    ## стандартные каналы вывода
  input_preferred: "voice"                    ## стандартный ввод
  greet_lang_key: "greet.hello_by_name"       ## i18n ключи на ваше усмотрение

steps:
  - name: audit
    call: system_audit.run
    save_as: audit

  - name: resolve_outputs
    call: io.resolve_outputs
    args:
      preferred: ${vars.outputs_preferred}
      available: ${audit.channels}
    save_as: outputs                 ## e.g. {"telegram": true, "voice": true}

  - name: get_user
    call: profile.get_user
    save_as: user

  - name: ensure_name
    when: ${!user.name}
    do:
      - name: ask_name_send
        parallel:
          - when: ${outputs.telegram}
            call: io.telegram.send
            args:
              text: "Как к вам обращаться?"
          - when: ${outputs.voice}
            call: io.voice.tts.speak
            args:
              text: "как к вам обращаться?"
      - name: ask_name_listen
        prefer_input: ${vars.input_preferred}   ## 'voice' → stt, иначе tg reply hook
        await:
          source: any
          timeout: "20s"
          save_as: name_answer
      - name: normalize_and_set
        when: ${name_answer.text}
        call: profile.set_name
        args:
          name: ${name_answer.text}

  - name: location
    try:
      call: location.current
      save_as: loc
    catch:
      set: {loc: null}

  - name: weather
    call: weather.brief
    args:
      location: ${loc}
    save_as: w

  - name: compose_message
    set:
      msg: |
        ${ user.name ? ("Привет, " + user.name + "!") : "Привет!" }
        ${ w.text }

  - name: deliver
    parallel:
      - when: ${outputs.telegram}
        call: io.telegram.send
        args:
          text: ${msg}
      - when: ${outputs.voice}
        call: io.voice.tts.speak
        args:
          text: ${msg}

  - name: telemetry
    call: observability.event
    args:
      name: "greet_on_boot.done"
      props:
        had_tg: ${outputs.telegram}
        had_voice: ${outputs.voice}
        had_name: ${user.name ? true : false}
on_error:
  - call: observability.event
    args:
      name: "greet_on_boot.error"
      props: {step: ${step.name}, error: ${error}}
  - call: inbox.local.save
    args: {scenario: "greet_on_boot", payload: ${error}}
```

## граф выполнения (Graphviz DOT)

```dot
digraph G {
  rankdir=LR; splines=true; node [shape=box, style=rounded];
  start -> audit -> resolve_outputs -> get_user -> ensure_name -> location -> weather -> compose -> deliver -> done;
  ensure_name [label="ensure_name?\n(ask/send → await → set)"];
  deliver [label="deliver\n(tg || voice)"];
}
```

## поведение по краям

* идемпотентность: ключ «на сегодня» на (node, user) — исключаем спам при рестарте сервиса.
* деградация: если `io.telegram` не сконфигурирован/не авторизован — шаг «deliver.tg» пропускается; аналогично для TTS.
* приватность: не логируем имя/текст целиком, в телеметрии только флаги.
* блокирующие ожидания: `await` с таймаутом; если имя не получено — продолжаем без имени, но помечаем в профиле «pending_name=true».

## что потребуется «доделать в ядре/портах»

* `io.resolve_outputs` — маленький сервис, который из «предпочтительных» и «доступных» строит карту каналов.
* хук «await any»: единый механизм ожидания ответа из голосового ввода или tg-реплая (в том числе через вебхук).
* хранение контекста: `ctx.user`, `ctx.node`, `ctx.lang`, `ctx.tz`, `ctx.location` — доступ из сценария.
* тонкая политика запуска: окно времени, лимиты на частоту, приоритет/очередь.

## быстрый smoke-test (cli)

```
adaos scenario run greet_on_boot --dry --ctx.user.id=me --ctx.node.id=node-1
adaos scenario run greet_on_boot --force
```

Если ок, могу перевести это в ваш текущий формат манифеста сценариев и накидать заглушки портов (`profile`, `io.voice`, `io.telegram`, `observability`, `location`) — чтобы сразу собрать минимальный happy-path.
