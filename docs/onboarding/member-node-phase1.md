# Member node onboarding (phase 1)

Goal: add a *member* node to an AdaOS hub using a short one-time join-code (no long-lived tokens in CLI args), then start `adaos api serve` via OS autostart/service when possible.

## Prereqs

- Python 3.11
- `adaos` installed (or run via repo bootstraps)
- Hub node is running `adaos api serve` and has role `hub`

## 1) On the hub: create a join-code

Run on the hub machine:

```bash
adaos hub join-code create
```

This prints a short code like `ABCD-EFGH` (one-time, TTL).

## 2) On the member: bootstrap + join + autostart

`RootUrl`/`--root-url` is the **join entrypoint** (Hub machine in local dev, or a Root proxy in future). You typically do **not** pass a hub URL manually: `adaos node join` stores the returned URL into `node.yaml`.

For offline/LAN-only setups you can still override the hub address explicitly via `adaos node join --hub-url http://<HUB_LAN_IP>:8777` (advanced).

### Windows (PowerShell, pip bootstrap)

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1 -JoinCode <CODE> -RootUrl http://<HUB_HOST>:8777
```

### Linux/macOS (pip bootstrap)

```bash
bash tools/bootstrap.sh --join-code <CODE> --root-url http://<HUB_HOST>:8777
```

### Windows/Linux/macOS (uv bootstrap)

```powershell
powershell -ExecutionPolicy Bypass -File tools/bootstrap_uv.ps1 -JoinCode <CODE> -RootUrl http://<HUB_HOST>:8777
```

```bash
bash tools/bootstrap_uv.sh --join-code <CODE> --root-url http://<HUB_HOST>:8777
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
adaos node status --control http://127.0.0.1:8777 --json
```

Expected (example):

- `role` is `member`
- `ready` is `true`
- `route_mode` is `ws` when connected to hub via `/ws/subnet`

## Where config is stored

- Node configuration: `$ADAOS_BASE_DIR/node.yaml` (default in bootstraps: `<repo>/.adaos/node.yaml`)
- Hub join-code store: `$ADAOS_BASE_DIR/join_codes.json` (hub side)

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
