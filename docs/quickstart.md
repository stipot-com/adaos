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
```

### Windows PowerShell with `uv`

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap_uv.ps1
.\.venv\Scripts\Activate.ps1
```

### Windows PowerShell with `pip`

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1
.\.venv\Scripts\Activate.ps1
```

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
curl -fsSL https://app.inimatic.com/assets/linux/init.sh | bash -s --
```

### Windows PowerShell

```powershell
iwr -UseBasicParsing https://app.inimatic.com/assets/windows/init.ps1 | iex; init.ps1
```

### Windows CMD

```bat
curl -fsSL -o init.bat https://app.inimatic.com/assets/windows/init.bat && init.bat
```

These scripts can optionally receive a join code for member-node onboarding.
