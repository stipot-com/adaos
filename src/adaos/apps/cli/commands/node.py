from __future__ import annotations

import json
import platform
from typing import Any

import requests
import typer

from adaos.services.node_config import load_config, save_config, set_role as cfg_set_role

app = typer.Typer(help="Node operations (join/status/role).")
role_app = typer.Typer(help="Manage local node role.")
app.add_typer(role_app, name="role")


def _print(data: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        typer.echo(str(data))


@app.command("join")
def node_join(
    code: str = typer.Option(..., "--code", help="Short one-time join-code"),
    root: str = typer.Option(..., "--root", help="Join endpoint base URL (Hub or Root proxy)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    """
    Join a subnet as member using a short one-time join-code.

    This stores returned subnet token + hub URL into node.yaml under the active base_dir.
    """
    cfg = load_config()
    url = root.rstrip("/") + "/api/node/join"
    payload = {
        "code": code,
        "node_id": cfg.node_id,
        "hostname": platform.node(),
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
    except Exception as exc:
        typer.secho(f"[AdaOS] join failed: {type(exc).__name__}: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc
    if resp.status_code != 200:
        try:
            body = resp.json()
        except Exception:
            body = (resp.text or "").strip()
        typer.secho(f"[AdaOS] join failed: HTTP {resp.status_code}", fg=typer.colors.RED)
        if body:
            typer.echo(body)
        raise typer.Exit(code=1)

    data = resp.json() or {}
    token = str(data.get("token") or "").strip()
    subnet_id = str(data.get("subnet_id") or "").strip()
    hub_url = str(data.get("hub_url") or root).strip()
    if not token or not subnet_id or not hub_url:
        typer.secho("[AdaOS] join failed: invalid response from server (missing token/subnet_id/hub_url)", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    cfg.token = token
    cfg.subnet_id = subnet_id
    cfg.hub_url = hub_url
    cfg.role = "member"
    save_config(cfg)

    out = {
        "ok": True,
        "node_id": cfg.node_id,
        "subnet_id": cfg.subnet_id,
        "role": cfg.role,
        "hub_url": cfg.hub_url,
    }
    _print(out, json_output=json_output)


@app.command("status")
def node_status(
    control: str = typer.Option("http://127.0.0.1:8777", "--control", help="Local control API base URL"),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="Probe local control API for readiness"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    cfg = load_config()
    result: dict[str, Any] = {
        "node_id": cfg.node_id,
        "subnet_id": cfg.subnet_id,
        "role": cfg.role,
        "hub_url": cfg.hub_url,
        "ready": None,
        "route_mode": None,
        "connected_to_hub": None,
    }
    if probe:
        url = control.rstrip("/") + "/api/node/status"
        headers = {"X-AdaOS-Token": cfg.token or "dev-local-token"}
        try:
            r = requests.get(url, headers=headers, timeout=2.5)
            if r.status_code == 200:
                js = r.json() or {}
                result["ready"] = bool(js.get("ready"))
                result["route_mode"] = js.get("route_mode")
                result["connected_to_hub"] = js.get("connected_to_hub")
        except Exception:
            pass
    _print(result, json_output=json_output)


@role_app.command("set")
def role_set(
    role: str = typer.Option(..., "--role", help="hub|member"),
    subnet_id: str | None = typer.Option(None, "--subnet-id"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    cfg = cfg_set_role(role, hub_url=None, subnet_id=subnet_id)
    out = {"ok": True, "node_id": cfg.node_id, "subnet_id": cfg.subnet_id, "role": cfg.role, "ready": None}
    _print(out, json_output=json_output)
