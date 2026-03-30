# SDK Context

SDK-код работает через runtime context, а не через неявное глобальное состояние процесса.

## Текущий паттерн

- runtime инициализирует context при старте CLI или API
- SDK-helper'ы читают из него пути, capabilities, event bus и environment data
- validation поднимает явные runtime errors, если context недоступен

## Почему это важно

Такой подход делает исполнение навыков и внутренний tooling более предсказуемыми и проще валидируемыми, чем свободная модель на глобальных импортах.
