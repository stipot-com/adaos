# Быстрый старт

## Требования

- Python `3.11`
- Git
- PowerShell на Windows или Bash на Linux/macOS

Опционально:

- `uv` для bootstrap-потока на Windows
- приватные submodule, если вы работаете еще и с клиентом, backend или infra-репозиториями

## Клонирование

```bash
git clone -b rev2026 https://github.com/stipot-com/adaos.git
cd adaos
```

Опциональные submodule:

```bash
git submodule update --init --recursive \
  src/adaos/integrations/adaos-client \
  src/adaos/integrations/adaos-backend \
  src/adaos/integrations/infra-inimatic
```

## Bootstrap

### Linux / macOS

```bash
bash tools/bootstrap.sh
source .venv/bin/activate
# bash tools/bootstrap.sh --zone ru --dev
```

### Windows PowerShell через `uv`

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap_uv.ps1
.\.venv\Scripts\Activate.ps1
# powershell -ExecutionPolicy Bypass -File tools/bootstrap_uv.ps1 -ZoneId ru -Dev
```

### Windows PowerShell через `pip`

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1
.\.venv\Scripts\Activate.ps1
# powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1 -ZoneId ru -Dev
```

Bootstrap-скрипты поддерживают zonal Root routing через `--zone` или `-ZoneId`. Используем только двухбуквенный код страны или региона, например `ru`. Это влияет на `adaos dev root init`, `adaos dev root login`, member join по join-code и создание join-code на hub, если используется стандартный публичный Root URL. Для национальных зон действует правило `[zone].api.inimatic.com`; сейчас это актуально для `ru`, поэтому будет выбран `https://ru.api.inimatic.com`, а остальные зоны пока остаются на `https://api.inimatic.com`. Дополнительно флаг `--dev` / `-Dev` записывает `ENV_TYPE=dev` в `.env`.

### Ручная editable-установка

```bash
pip install -e ".[dev]"
```

## Первые команды

```bash
adaos --help
adaos where
adaos api serve --host 127.0.0.1 --port 8777
```

Во втором терминале:

```bash
curl -i http://127.0.0.1:8777/health/live
curl -i http://127.0.0.1:8777/health/ready
```

## Типовые локальные действия

Установить стандартный локальный контент:

```bash
adaos install
adaos update
```

Проверить локальные объекты:

```bash
adaos skill list
adaos scenario list
adaos node status --json
```

Запустить тесты:

```bash
pytest
```
