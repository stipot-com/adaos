# SDK Decorators

AdaOS использует decorator'ы из `adaos.sdk.core.decorators`, чтобы прикреплять machine-readable metadata к tool-функциям.

## Текущая роль

Decorator layer нужен для того, чтобы:

- описывать входные и выходные схемы tools
- добавлять summary и stability metadata
- поддерживать export для LLM-facing и control-plane описаний

## Связанные команды

CLI предоставляет SDK export helper'ы:

```bash
adaos sdk export
adaos sdk export-all
adaos sdk check
```
