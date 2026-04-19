# Runtime и операции

## Инспекция runtime

```bash
adaos runtime status
adaos runtime logs
adaos runtime memory-status
adaos runtime memory-telemetry --limit 20
adaos runtime memory-incidents --limit 20
adaos runtime memory-sessions
adaos runtime memory-session <SESSION_ID>
adaos runtime memory-artifact <SESSION_ID> <ARTIFACT_ID>
adaos runtime memory-profile-start --profile-mode sampled_profile
adaos runtime memory-profile-stop <SESSION_ID>
adaos runtime memory-profile-retry <SESSION_ID>
adaos runtime memory-publish <SESSION_ID>
adaos node status
adaos node reliability
```

Эти команды полезны для проверки local readiness, runtime slots и общей модели health узла.
Если `node reliability` во время контролируемого restart fallback'ится на supervisor, команда также печатает compact public memory summary, когда она доступна.
Команды `runtime memory-*` напрямую открывают supervisor-owned profiling workflow через memory API.
В текущем Phase 2 baseline `memory-profile-start` создаёт requested profiling session, а supervisor переводит runtime в нужный mode через controlled restart.
`memory-session` теперь показывает compact operation/telemetry/artifact context для одной profiling session, `memory-telemetry` даёт быстрый tail rolling growth samples, а `memory-incidents` и `memory-artifact` упрощают разбор завершённых и failed profiling incidents без ручного чтения state files. Если profiling session завершилась `failed`, `cancelled` или `stopped`, `memory-profile-retry` создаёт новую requested session с тем же profile mode и сохраняет retry-chain metadata.
`memory-publish` теперь запускает Phase 3 publication path для session summary и печатает `published_ref`, если root принял report.

## Autostart и service mode

```bash
adaos autostart status
adaos autostart inspect
adaos autostart enable
adaos autostart disable
```

`autostart` — основной operational path для запуска AdaOS как управляемого системного сервиса.

`autostart inspect` помогает разбирать ситуации, когда hub «жив», но UI timeout'ится или одно CPU-ядро забито:
команда печатает bind автозапуска, активный PID, самые «горячие» дочерние процессы и запущенные service-skills.
Когда активен supervisor mode, она также показывает состояние managed runtime/candidate slot и последние причины start/stop,
что полезно при проверке slot migration, candidate prewarm и restart handoff.

## Управление обновлением ядра

```bash
adaos autostart update-status
adaos autostart update-start
adaos autostart update-cancel
adaos autostart update-defer --delay-sec 300
adaos autostart update-rollback
adaos autostart update-promote-root
adaos autostart update-restore-root --backup-dir <PATH>
adaos autostart update-complete
adaos autostart smoke-update
```

Эти команды интегрированы с lifecycle runtime и endpoint'ами `/api/admin/update/*`.

В service mode authoritative update surface — supervisor, а не временный runtime listener:

- production runtime запускается из active slot manifest, а не из root checkout
- `update-status` должен оставаться доступным через supervisor-backed state даже пока `:8777` перезапускается
- root/bootstrap code может быть promoted после успешной slot validation, но restarted production runtime всё равно идёт из slot `A|B`
- `update-status` может также показывать compact Phase 1 supervisor memory summary, чтобы оператор видел profiling intent/session state в том же rollout-окне

Текущий autostart-managed flow для bootstrap/self-update:

1. запустить `adaos autostart update-start`
2. дождаться slot validation; если bootstrap-managed files изменились, supervisor автоматически выполнит root promotion и запросит restart autostart-сервиса
3. во время этого handoff `update-status` может кратко показывать `phase: root_promotion_pending`, затем `phase: root_promoted` / `supervisor attempt: awaiting_root_restart`, пока перезапущенный сервис не сойдётся уже под новым root-based supervisor/bootstrap code

Если supervisor применяет minimum interval между update'ами, `update-start` может вернуть planned transition вместо немедленного countdown. Тогда:

1. `update-status` показывает `state: planned` и `scheduled for: ...`
2. ещё один update signal обновляет или аннотирует queued plan вместо запуска второй параллельной transition
3. `adaos autostart update-defer --delay-sec <sec>` может сдвинуть scheduled window вперёд
4. если второй signal приходит, пока реальная transition уже активна, supervisor записывает `subsequent transition: queued` и выполняет её один раз после завершения текущей transition

`update-promote-root` создаёт backup snapshot заменяемых bootstrap-managed files перед копированием их из validated active slot в root checkout.
`update-complete` теперь в основном retry/compatibility команда:

- на текущих autostart-managed Linux deployment'ах supervisor сам пытается завершить root promotion и запросить restart сервиса
- `update-complete` сначала просит supervisor завершить этот flow server-side
- если running supervisor старый или не умеет self-request restart, CLI fallback'ится на legacy operator path и явно перезапускает autostart service

Если root promotion уже завершён и pending остаётся только supervisor/bootstrap restart, повторный `update-complete` ретраит только restart и не делает root promotion заново.
Если promoted supervisor/bootstrap revision не вернулся чисто, `update-restore-root --backup-dir <PATH>` восстанавливает root checkout из backup snapshot без требования live supervisor process. Добавьте `--restart`, чтобы сразу запросить autostart service restart после restore.

## Операции hub/member

```bash
adaos hub root reports --kind memory-profile
adaos hub root reports --kind memory-profile --state finished --suspected-only
adaos hub root memory-session <SESSION_ID>
adaos hub root memory-artifacts <SESSION_ID>
adaos hub root memory-artifact <SESSION_ID> <ARTIFACT_ID>
adaos hub root memory-artifact-pull <SESSION_ID> <ARTIFACT_ID>
adaos hub join-code create
adaos hub root status
adaos hub root reconnect
adaos node join --join-code <CODE>
adaos node role set --role member
```

`adaos hub root reports --kind memory-profile` — первый operator-facing retrieval path в рамках Phase 3 для remotely published memory-profile summaries. Он дополняет локальные `runtime memory-*` команды: показывает, что root уже успел ingest'нуть для конкретного hub, поддерживает `--session-id`, а также compact remote filtering вроде `--state finished --suspected-only`.
`adaos hub root memory-session <SESSION_ID>` открывает один опубликованный profiling incident и печатает compact RSS / retry / artifact summary без raw JSON.
`adaos hub root memory-artifacts <SESSION_ID>` показывает remote artifact catalog вместе с publish-policy status вроде `inline_available`, `size_limit_exceeded` или `kind_not_allowed`.
`adaos hub root memory-artifact <SESSION_ID> <ARTIFACT_ID>` теперь возвращает нормализованный root-side delivery contract: для inline artifacts это `delivery mode: root_inline_content` плюс transfer metadata, а для local-only artifacts — fetch strategy, source control path и признак relay readiness.
`adaos hub root memory-artifact-pull <SESSION_ID> <ARTIFACT_ID>` исполняет этот контракт: сначала пробует root, а если artifact не хранится inline на root, fallback'ится на current hub control API и забирает данные с transfer metadata (`json`, `utf-8` или `base64`) и chunk limits через `--max-bytes`.

## Операции с Yjs webspace

```bash
adaos node yjs status
adaos node yjs create --webspace default
adaos node yjs describe --webspace default
adaos node yjs scenario --webspace default --scenario-id web_desktop
```

Группа `node yjs` сейчас является основным operator-facing интерфейсом для synchronized webspace и desktop state.
