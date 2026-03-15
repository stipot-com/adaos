# Member node onboarding (phase 1)

Goal: add a *member* node to an AdaOS hub using a short one-time join-code (no long-lived tokens in CLI args), then start `python -m adaos api serve` via OS autostart/service when possible.

## Prereqs

- Python 3.11
- `adaos` installed (or run via repo bootstraps)
- Hub node is running `python -m adaos api serve` (or bootstrap-installed autostart) and has role `hub`

## 1) On the hub: create a join-code (Root session)

Run on the hub machine:

```bash
python -m adaos hub join-code create
```

This prints a short code like `ABCD-EFGH` (one-time, TTL). The code is stored on **Root** and can be consumed by the member via Root.

Notes:

- Root mode does **not** require a public hub URL: members connect via the Root proxy path `/hubs/<subnet_id>/...`.
- Hub must be bootstrapped on Root (mTLS hub cert/key + Root CA). If missing, run: `python -m adaos dev root init` (requires `ROOT_TOKEN`).

## 1b) Offline/LAN-only join-code (no Root)

If the subnet has no Internet access / no Root connectivity, create a *local* join-code on the hub:

```bash
python -m adaos hub join-code create --local
```

## 2) On the member: bootstrap + join + autostart

`RootUrl`/`--root-url` is the **join entrypoint**:

- Online mode: Root (default: `https://api.inimatic.com`)
- Offline mode: Hub URL (the hub node accepts join directly)

For offline/LAN-only setups you can still override the hub address explicitly via `adaos node join --hub-url http://<HUB_LAN_IP>:8777` (advanced).

### Windows (PowerShell, pip bootstrap)

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1 -JoinCode <CODE>
```

### Linux/macOS (pip bootstrap)

```bash
bash tools/bootstrap.sh --join-code <CODE>
```

### Windows/Linux/macOS (uv bootstrap)

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap_uv.ps1 -JoinCode <CODE>
```

```bash
bash tools/bootstrap_uv.sh --join-code <CODE>
```

### Offline/LAN member bootstrap (using `--local` join-code)

When using a local hub join-code, you must point join to the hub URL explicitly:

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1 -JoinCode <CODE> -RootUrl http://<HUB_HOST>:8777
```

```bash
bash tools/bootstrap.sh --join-code <CODE> --root-url http://<HUB_HOST>:8777
```

## Note: joining on the same machine as the hub

If the hub is already running on `127.0.0.1:8777`, the member node must use a different local API port.

Bootstraps now auto-pick `8778+` when `--join-code`/`-JoinCode` is provided, but you can override explicitly:

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1 -JoinCode <CODE> -RootUrl http://127.0.0.1:8777 -ServePort 8778 -ControlPort 8778
```

## 3) Verify status

On the member machine:

```bash
python -m adaos node status --control http://127.0.0.1:8777 --json
```

Expected (example):

- `role` is `member`
- `ready` is `true`
- `route_mode` is `ws` when connected to hub via `/ws/subnet`

## Where config is stored

- Node configuration: `$ADAOS_BASE_DIR/node.yaml` (default in bootstraps: `<repo>/.adaos/node.yaml`)
- Join-code store: Root server (Root mode) or `$ADAOS_BASE_DIR/join_codes.json` (hub local mode)

## Idempotency

- Re-running bootstrap without `--join-code` should keep existing `node_id` and config.
- Join-codes are one-time; reusing the same code fails.

## Default role behavior

- If `--join-code`/`-JoinCode` is provided, bootstrap defaults role to `member`.
- If no join-code is provided, bootstrap defaults role to `hub`.

## Windows note: prefer `python -m adaos` (or wrapper script)

If `adaos` console-script wrapper is broken (e.g. `ModuleNotFoundError: No module named 'adaos'`), run via:

```powershell
.\.venv\Scripts\python.exe -m adaos --help
.\tools\adaos.ps1 hub join-code create
```

For the hub API on Windows, prefer:

```powershell
.\.venv\Scripts\python.exe -m adaos api serve --host 127.0.0.1 --port 8777
```
