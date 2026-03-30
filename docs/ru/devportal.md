# DevPortal

`adaos dev` содержит developer workflow для Root-backed и Forge-style окружений.

## Основные группы команд

- `adaos dev root`: инициализация, логин и просмотр Root-side логов
- `adaos dev skill`: create, push, validate, publish, prep, run и test owner skills
- `adaos dev scenario`: create, push, validate, publish, run и test owner scenarios

## Типовой поток

```bash
adaos dev root init
adaos dev root login
adaos dev skill create demo_skill
adaos dev skill push demo_skill
adaos dev skill publish demo_skill
```

## Важное различие

Эти команды не равны локальному workspace lifecycle:

- `adaos skill ...` и `adaos scenario ...` работают с локальным workspace/runtime
- `adaos dev skill ...` и `adaos dev scenario ...` предназначены для Root-backed developer workflow
