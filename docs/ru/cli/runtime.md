# Runtime и операции

## Инспекция runtime

```bash
adaos runtime status
adaos runtime logs
adaos node status
adaos node reliability
```

Эти команды полезны для проверки local readiness, runtime slots и общего health-моделя узла.

## Autostart и service mode

```bash
adaos autostart status
adaos autostart inspect
adaos autostart enable
adaos autostart disable
```

`autostart` - это основной operational path для запуска AdaOS как управляемого сервиса ОС.

`autostart inspect` помогает отладить ситуации, когда hub "жив", но UI таймаутится или одно ядро CPU забито:
команда печатает bind автозапуска, активный PID, самые "горячие" дочерние процессы и запущенные service-skills.

## Управление обновлением ядра

```bash
adaos autostart update-status
adaos autostart update-start
adaos autostart update-cancel
adaos autostart update-rollback
adaos autostart smoke-update
```

Эти команды интегрированы с runtime lifecycle и endpoint'ами `/api/admin/update/*`.

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
