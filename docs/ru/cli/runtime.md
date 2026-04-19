# Runtime и операции

## Инспекция runtime

```bash
adaos runtime status
adaos runtime logs
adaos runtime memory-status
adaos runtime memory-sessions
adaos runtime memory-session <SESSION_ID>
adaos runtime memory-profile-start --profile-mode sampled_profile
adaos runtime memory-profile-stop <SESSION_ID>
adaos runtime memory-publish <SESSION_ID>
adaos node status
adaos node reliability
```

Эти команды полезны для проверки local readiness, runtime slots и общей health-модели узла.
Если `node reliability` во время контролируемого restart fallback'ится на supervisor, команда также печатает compact public Phase 1 memory summary.
Команды `runtime memory-*` напрямую открывают Phase 1 profiling workflow через supervisor-owned API; в рамках Phase 1 это всё ещё `intent-only` controls и они пока не означают автоматический restart-into-profile.

## Autostart и service mode

```bash
adaos autostart status
adaos autostart inspect
adaos autostart enable
adaos autostart disable
```

`autostart` — это основной operational path для запуска AdaOS как управляемого сервиса ОС.

`autostart inspect` помогает отладить ситуации, когда hub "жив", но UI таймаутится или одно ядро CPU забито:
команда печатает bind автозапуска, активный PID, самые "горячие" дочерние процессы и запущенные service-skills.
Когда supervisor mode активен, она также печатает состояние managed runtime/candidate slot и последние причины start/stop,
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

Эти команды интегрированы с runtime lifecycle и endpoint'ами `/api/admin/update/*`.

В service mode authoritative update surface — supervisor, а не временный runtime listener:

- production runtime запускается из active slot manifest, а не из root checkout
- `update-status` должен оставаться доступным через supervisor-backed state даже пока `:8777` перезапускается
- root/bootstrap code может быть promoted после успешной slot validation, но restarted production runtime всё равно идёт из slot `A|B`
- `update-status` также может показывать compact Phase 1 memory summary, чтобы оператор видел profiling intent/session state в том же rollout окне

Текущий autostart-managed flow для bootstrap/self-update:

1. запустить `adaos autostart update-start`
2. дождаться slot validation; если bootstrap-managed files изменились, supervisor автоматически пройдёт root promotion и запросит restart autostart-сервиса
3. во время этого handoff `update-status` может кратко показывать `phase: root_promotion_pending`, затем `phase: root_promoted` / `supervisor attempt: awaiting_root_restart`, пока перезапущенный сервис не сойдётся уже под новым root-based supervisor/bootstrap code

Если supervisor применяет minimum interval между update'ами, `update-start` может вернуть planned transition вместо немедленного countdown. Тогда:

1. `update-status` показывает `state: planned` и `scheduled for: ...`
2. ещё один update signal обновляет или аннотирует queued plan вместо запуска второй параллельной transition
3. `adaos autostart update-defer --delay-sec <sec>` может сдвинуть scheduled window вперёд
4. если второй signal приходит, пока реальная transition уже активна, supervisor записывает `subsequent transition: queued` и выполняет её один раз после завершения текущей transition

`update-promote-root` создаёт backup snapshot заменяемых bootstrap-managed files перед копированием их из validated active slot в root checkout.
`update-complete` теперь в основном retry/compatibility команда:

- на текущих autostart-managed Linux deployments supervisor сам пытается завершить root promotion и запросить restart сервиса
- `update-complete` сначала просит supervisor завершить этот flow server-side
- если running supervisor старый или не умеет self-request restart, CLI fallback'ится на legacy operator path и явно перезапускает autostart service

Если root promotion уже завершён и pending остаётся только supervisor/bootstrap restart, повторный `update-complete` ретраит только restart и не делает root promotion заново.
Если promoted supervisor/bootstrap revision не вернулся чисто, `update-restore-root --backup-dir <PATH>` восстанавливает root checkout из backup snapshot без требования live supervisor process. Добавьте `--restart`, чтобы сразу запросить autostart service restart после restore.

## Операции hub/member

```bash
adaos hub join-code create
adaos hub root status
adaos hub root reconnect
adaos node join --join-code <CODE>
adaos node role set --role member
```

## Операции с Yjs webspace

```bash
adaos node yjs status
adaos node yjs create --webspace default
adaos node yjs describe --webspace default
adaos node yjs scenario --webspace default --scenario-id web_desktop
```

Группа `node yjs` сейчас является основным операторским интерфейсом для synchronized webspace и desktop state.
