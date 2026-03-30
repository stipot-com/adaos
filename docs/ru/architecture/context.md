# Agent Context

## Назначение

`AgentContext` - это общий runtime-контейнер, который используют CLI и API-сервер. Он дает командам и сервисам единый источник правды для путей, настроек, репозиториев, event bus и capability checks.

## Как он инициализируется

- CLI инициализирует или пересобирает context в `src/adaos/apps/cli/app.py`
- API-сервер поднимает его на старте до монтирования router'ов
- environment variables и `.env` применяются до запуска runtime-сервисов

## Что он предоставляет

Обычно context содержит:

- вычисленные пути к workspace, state и runtime directory
- settings из файлов и окружения
- репозитории навыков и сценариев
- SQLite-доступ
- git-helper'ы
- event bus
- capability и security helper'ы

## Почему это важно

Такой дизайн выравнивает локальные команды и API-операции:

- используются одни и те же registry и repository
- команды могут переключаться между прямым исполнением и API-backed режимом
- bootstrap и подготовка окружения сосредоточены в одном месте
