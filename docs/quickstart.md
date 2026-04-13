# Quickstart

## Requirements

- Python `3.11`
- Git
- PowerShell on Windows, or Bash on Linux/macOS

Optional components:

- `uv` for the Windows bootstrap flow
- private submodules if you also work on the client, backend, or infrastructure repositories

## Clone the repository

```bash
git clone -b rev2026 https://github.com/stipot-com/adaos.git
cd adaos
```

Optional submodules:

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

### Windows PowerShell with `uv`

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap_uv.ps1
.\.venv\Scripts\Activate.ps1
# powershell -ExecutionPolicy Bypass -File tools/bootstrap_uv.ps1 -ZoneId ru -Dev
```

### Windows PowerShell with `pip`

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1
.\.venv\Scripts\Activate.ps1
# powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1 -ZoneId ru -Dev
```

Bootstrap scripts support zone-aware Root routing via `--zone` or `-ZoneId`. Use only a two-letter country or region code such as `ru`. This affects hub bootstrap (`adaos dev root init`), owner login (`adaos dev root login`), member join via join-code, and hub join-code creation when the default public Root URL is in use. National zones follow the `[zone].api.inimatic.com` rule; right now `ru` becomes `https://ru.api.inimatic.com`, while the other zones still stay on `https://api.inimatic.com`. The optional `--dev` / `-Dev` flag writes `ENV_TYPE=dev` into `.env`.

### Manual editable install

```bash
pip install -e ".[dev]"
```

## First commands

```bash
adaos --help
adaos where
adaos api serve --host 127.0.0.1 --port 8777
```

In a second terminal:

```bash
curl -i http://127.0.0.1:8777/health/live
curl -i http://127.0.0.1:8777/health/ready
```

## Common local workflows

Install default local content:

```bash
adaos install
adaos update
```

Inspect local assets:

```bash
adaos skill list
adaos scenario list
adaos node status --json
```

Run the test suite:

```bash
pytest
```

## Init scripts for node bootstrap

AdaOS also supports one-line bootstrap flows used by hosted onboarding:

### Linux

```bash
curl -fsSL https://myinimatic.web.app/assets/linux/init.sh | bash -s --
# curl -fsSL https://myinimatic.web.app/assets/linux/init.sh | bash -s -- --zone ru
```

### Windows PowerShell

```powershell
iwr -UseBasicParsing https://myinimatic.web.app/assets/windows/init.ps1 | iex; init.ps1
# iwr -UseBasicParsing https://myinimatic.web.app/assets/windows/init.ps1 | iex; init.ps1 -ZoneId ru
```

### Windows CMD

```bat
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "iwr -UseBasicParsing 'https://myinimatic.web.app/assets/windows/init.ps1' -OutFile '.\\init.ps1'" && powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\init.ps1
# powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "iwr -UseBasicParsing 'https://myinimatic.web.app/assets/windows/init.ps1' -OutFile '.\\init.ps1'" && powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\init.ps1 -ZoneId ru
```
These scripts can optionally receive a join code for member-node onboarding and a zone identifier for zonal Root routing.
