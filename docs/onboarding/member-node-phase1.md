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

