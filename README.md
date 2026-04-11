# AdaOS

![AdaOS CI](https://github.com/stipot-com/adaos/actions/workflows/ci.yml/badge.svg)

AdaOS is a Python-based platform for building distributed assistant systems composed of skills, scenarios, and node services. It provides a local CLI, an HTTP API, runtime services, and developer tooling for bootstrapping, testing, orchestration, and automation across local, private, and hybrid environments.

[Documentation](https://stipot-com.github.io/adaos/)

## Overview

AdaOS is designed around a small core and extensible edges:

- `skills` implement focused capabilities such as integrations, automations, interface logic, or assistant behaviors
- `scenarios` compose workflows and user-facing flows on top of runtime services and skills
- `nodes` can run as hubs or members inside a larger AdaOS network
- `SDK`, `CLI` and `API` provide local control, automation, diagnostics, and operational workflows

This repository contains the open developer platform: core runtime components, SDK modules, bootstrap scripts, documentation, and tests for local development, experimentation, and integration work.

## Why AdaOS

Most real-world assistant and automation tasks do not live inside a single chat window or a single device. They span browsers, local services, devices, users, and external systems. AdaOS is built to support this kind of distributed environment with a model based on composable skills, scenarios, and node roles.

AdaOS is intended for:

- local and private assistant deployments
- distributed automation and service orchestration
- experimental multi-node assistant environments
- university labs, applied research, and student-built products
- integrators and teams building domain-specific assistant solutions

## Features

- Python 3.11 CLI exposed as `adaos`
- Local HTTP API for node and runtime control
- Skill and scenario development workflows
- Bootstrap scripts for Linux, macOS, and Windows
- Runtime support for hub/member node roles
- Developer-oriented project structure for extensions, testing, and automation
- Optional submodules for related client, backend, and infrastructure work
- MkDocs-based documentation site

## What You Can Build

With AdaOS you can:

- develop and run skills for integrations, automation, and assistant behavior
- compose scenarios that coordinate multi-step flows across services and nodes
- expose runtime functionality through a local API and CLI
- experiment with distributed assistant topologies using hub and member roles
- prototype local or private digital assistant environments
- use the platform as a base for campus, lab, and applied product development

## Quick Start

## Install from a single line (init scripts)

### Linux

```bash
# use optional key to join member to subnet: --join-code CODE 
curl -fsSL https://app.inimatic.com/assets/linux/init.sh | bash -s --
# pick zone explicitly when needed:
# curl -fsSL https://app.inimatic.com/assets/linux/init.sh | bash -s -- --zone ru
```

### Windows (PowerShell)

```powershell
# use optional key to join member to subnet: -JoinCode CODE
iwr -UseBasicParsing https://app.inimatic.com/assets/windows/init.ps1 | iex; init.ps1
# pick zone explicitly when needed:
# iwr -UseBasicParsing https://app.inimatic.com/assets/windows/init.ps1 | iex; init.ps1 -ZoneId ru
```

### Windows (CMD / .bat)

```bat
# use optional key to join member to subnet: -JoinCode CODE
curl -fsSL -o init.bat https://app.inimatic.com/assets/windows/init.bat && init.bat -JoinCode CODE
REM pick zone explicitly when needed:
REM curl -fsSL -o init.bat https://app.inimatic.com/assets/windows/init.bat && init.bat -ZoneId ru
```

## AdaOS Service management

```bash
# Restart
systemctl --user daemon-reload
systemctl --user restart adaos.service
# Проверить
systemctl --user status adaos.service --no-pager
journalctl --user -u adaos.service -n 120 --no-pager
curl http://127.0.0.1:8777/api/node/status (с X-AdaOS-Token, если требуется)
adaos autostart enable
adaos autostart status
adaos autostart update-complete
adaos autostart inspect
adaos autostart inspect --json
adaos autostart disable


adaos autostart update-status
adaos autostart update-start
# Checkup
cat ~/adaos/.adaos/state/core_update/status.json
cat ~/.adaos/state/core_update/status.json
adaos autostart update-cancel
adaos autostart update-rollback
adaos autostart smoke-update

adaos autostart update-status --json
adaos autostart smoke-update --countdown-sec 5 --json
adaos autostart update-cancel --json
adaos autostart update-rollback --json

# Recommended smoke-order:
adaos autostart update-status --json
adaos autostart smoke-update --countdown-sec 30 --json
adaos autostart update-cancel --json
adaos autostart smoke-update --countdown-sec 5 --json


adaos node reliability
adaos node status
adaos node status --probe
adaos hub root watch
adaos hub root reconnect
adaos hub root reconnect --transport ws|tcp

adaos autostart inspect
adaos autostart inspect --json
adaos autostart inspect --sample-sec 0.5

# CPU inspection
adaos autostart inspect --json
runtime_pid = runtime_process.pid
/root/adaos/.adaos/state/core_slots/slots/A/venv/bin/python -m pip install py-spy
/root/adaos/.adaos/state/core_slots/slots/A/venv/bin/python -m pip install 'websockets>=13,<16'
/root/adaos/.adaos/state/core_slots/slots/A/venv/bin/py-spy dump --pid <runtime_pid>

# Boot debug 
# https://myinimatic.web.app/?boot_debug=1

```

### Clone

```bash
git clone -b rev2026 https://github.com/stipot-com/adaos.git
cd adaos
```

### Linux / macOS

```bash
bash tools/bootstrap.sh
source .venv/bin/activate
adaos --help
# bash tools/bootstrap.sh --zone ru --dev
```

### Windows PowerShell

Using `uv`:

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap_uv.ps1
.\.venv\Scripts\Activate.ps1
adaos --help
# powershell -ExecutionPolicy Bypass -File tools/bootstrap_uv.ps1 -ZoneId ru -Dev
```

Using `pip`:

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1
.\.venv\Scripts\Activate.ps1
adaos --help
# powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1 -ZoneId ru -Dev
```

Repo bootstrap scripts support zone-aware Root routing via `--zone` or `-ZoneId`. Use only a two-letter country or region code such as `ru`. When the default public Root URL is in use, national zones follow the `[zone].api.inimatic.com` rule; right now `ru` resolves to `https://ru.api.inimatic.com`, while the other zones still stay on `https://api.inimatic.com`. The optional `--dev` / `-Dev` flag writes `ENV_TYPE=dev` into `.env`.

### Install from an existing environment

If you already have Python 3.11 and want a manual editable install:

```bash
pip install -e ".[dev]"
adaos --help
```

## Common Commands

```bash
adaos --help
adaos api serve --host 127.0.0.1 --port 8777
adaos skill list
adaos skill run weather_skill --topic nlp.intent.weather.get --payload '{"city":"Berlin"}'
```

Health endpoints are available once the API is running:

```bash
curl -i http://127.0.0.1:8777/health/live
curl -i http://127.0.0.1:8777/health/ready
```

## Documentation

- Project docs: `docs/`
- Quick start: `docs/quickstart.md`
- Architecture overview: `docs/architecture/overview.md`
- CLI reference: `docs/reference/cli.md`
- SDK docs: `docs/sdk/`
- English docs: `docs/en/`

## Scope and Status

AdaOS is an evolving platform. This repository focuses on the developer-facing and runtime foundations needed to build, test, and operate skills, scenarios, and node services.

Some ecosystem layers may evolve separately from this repository, including hosted or managed infrastructure, trust and publication workflows, operator tooling, and broader distribution services. The current repository should be understood as the core development and runtime foundation rather than the entirety of the AdaOS ecosystem.

## Development

### Run tests

```bash
pytest
```

### Build documentation locally

```bash
pip install -r requirements-docs.txt
mkdocs serve
```

### Project layout

```text
src/adaos/        Core package, apps, services, SDK, templates
tests/            Test suite
docs/             Documentation source
tools/            Bootstrap and diagnostic scripts
```

## Contributing

Contributions are welcome in areas such as:

- core runtime improvements
- skills and scenario tooling
- documentation
- developer workflows
- tests and diagnostics
- extension contracts and platform-facing specifications

For larger architectural changes, please open an issue or discussion first so proposals can be aligned with the platform direction.

## License

MIT. See [LICENSE](LICENSE).
