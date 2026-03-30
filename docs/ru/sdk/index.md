# SDK

SDK AdaOS - это Python-facing слой, который используют навыки и внутренний runtime-код.

## Основные области в текущем дереве

- `adaos.sdk.manage`: management helper'ы для skills, scenarios, resources и environment
- `adaos.sdk.data`: helper'ы для context, memory, events, environment, secrets и profile data
- `adaos.sdk.scenarios`: runtime и workflow helper'ы для сценариев
- `adaos.sdk.io`: IO-related helper'ы
- `adaos.sdk.web`: helper'ы для webspace и desktop
- `adaos.sdk.core`: decorators, context access, errors, exporter и validation

## Цели дизайна

- сделать skill-facing helper'ы легкими
- держать runtime contracts явными
- поддерживать validation и export tool metadata
- дать достаточную структуру для developer tooling и control-plane integration
