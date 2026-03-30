# SDK Adapters

SDK располагается поверх concrete adapter'ов. В текущей реализации именно адаптеры обеспечивают файловую систему, базу данных, git, audio, secrets и bridge-поведение, на которое опираются сервисы.

## Примеры в репозитории

- filesystem path provider'ы
- SQLite registry и store
- secure git и workspace helper'ы
- TTS и STT adapters
- secret backend'ы
- in-process skill context adapters

## Рекомендация

Если вы расширяете AdaOS, лучше добавлять новую инфраструктурную логику в `adapters/`, а затем открывать ее через services или SDK-helper'ы, вместо того чтобы связывать новый код напрямую с entry point команды.
