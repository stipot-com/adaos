# Quickstart

## Requirements

- Python `3.11.9+`
- Git
- Windows PowerShell `5.1+` or PowerShell `7+` on Windows, or Bash on Linux/macOS

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

Notes about local ports:

- `8777` is the default local API port for direct development.
- `8778` is reserved for the second slot in supervisor-managed mode.
- If you want the browser app to avoid auto-discovering your local runtime, start on a non-discoverable port such as `8779`:

```bash
adaos api serve --host 127.0.0.1 --port 8779
```

- When you pass an explicit port to `adaos api serve`, AdaOS persists it as `local_api_url` in `.adaos/node.yaml`, and later `adaos api serve` runs reuse it.
- `adaos api serve` is a direct development runtime and does not run supervisor-managed slot cutover logic.

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
curl -fsSL https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/linux/init.sh | bash -s --
# curl -fsSL https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/linux/init.sh | bash -s -- --zone ru
```

### Windows PowerShell

```powershell
# requires Windows PowerShell 5.1+ or PowerShell 7+
& ([scriptblock]::Create((iwr -UseBasicParsing https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1).Content))
# & ([scriptblock]::Create((iwr -UseBasicParsing https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1).Content)) -ZoneId ru
```

### Windows CMD

```bat
REM requires Windows PowerShell 5.1+ or PowerShell 7+
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "iwr -UseBasicParsing 'https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1' -OutFile '.\\init.ps1'" && powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\init.ps1
# powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "iwr -UseBasicParsing 'https://raw.githubusercontent.com/stipot-com/adaos/rev2026/tools/init/windows/init.ps1' -OutFile '.\\init.ps1'" && powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\init.ps1 -ZoneId ru
```
These scripts can optionally receive a join code for member-node onboarding and a zone identifier for zonal Root routing.
