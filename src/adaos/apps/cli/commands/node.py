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
    hub_url: str | None = typer.Option(None, "--hub-url", help="Optional explicit Hub URL override (offline/LAN setups)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    """
    Join a subnet as member using a short one-time join-code.

    This stores returned subnet token + hub URL into node.yaml under the active base_dir.
    """
    cfg = load_config()

    root_base = root.rstrip("/")
    candidates = [
        # Root-mediated join (preferred): Root issues/validates join-code and returns hub rendezvous.
        root_base + "/v1/subnets/join",
        # Legacy / offline: join directly against a hub node that holds the join-code locally.
        root_base + "/api/node/join",
    ]
    payload = {
        "code": code,
        "node_id": cfg.node_id,
        "hostname": platform.node(),
    }
    sess = requests.Session()
    try:
        sess.trust_env = False
    except Exception:
        pass

    def _is_missing_route(resp: requests.Response) -> bool:
        if resp.status_code not in (404, 405):
            return False
        try:
            js = resp.json()
        except Exception:
            txt = (resp.text or "").lower()
            return "cannot post" in txt or "not found" in txt
        if isinstance(js, dict):
            detail = js.get("detail")
            return detail == "Not Found"
        return False

    resp = None
    used_url = None
    last_body: Any = None
    for url in candidates:
        try:
            r = sess.post(url, json=payload, timeout=10)
        except Exception as exc:
            typer.secho(f"[AdaOS] join failed: {type(exc).__name__}: {exc}", fg=typer.colors.RED)
            typer.echo(f"url: {url}")
            raise typer.Exit(code=2) from exc
        if r.status_code == 200:
            resp = r
            used_url = url
            break
        # Route not available on this server; try next candidate.
        if _is_missing_route(r):
            continue
        resp = r
        used_url = url
        try:
            last_body = r.json()
        except Exception:
            last_body = (r.text or "").strip()
        break

    if resp is None or used_url is None:
        typer.secho("[AdaOS] join failed: no join endpoint found on root URL", fg=typer.colors.RED)
        for url in candidates:
            typer.echo(f"url: {url}")
        typer.echo("hint: pass --root http://<HUB_HOST>:8777 for direct hub join (offline/local dev), or update Root to a build that supports /v1/subnets/join.")
        raise typer.Exit(code=1)

    if resp.status_code != 200:
        if last_body is None:
            try:
                last_body = resp.json()
            except Exception:
                last_body = (resp.text or "").strip()
        typer.secho(f"[AdaOS] join failed: HTTP {resp.status_code}", fg=typer.colors.RED)
        typer.echo(f"url: {used_url}")
        if last_body:
            typer.echo(last_body)
        if resp.status_code == 404 and isinstance(last_body, dict) and last_body.get("detail") == "join-code not found":
            typer.echo("hint: if the code was created in hub local mode (--local), join against the hub URL: --root http://<HUB_HOST>:8777")
        raise typer.Exit(code=1)

    data = resp.json() or {}
    token = str(data.get("token") or "").strip()
    subnet_id = str(data.get("subnet_id") or "").strip()
    rendezvous_url = str(data.get("hub_url") or root).strip()
    if hub_url:
        rendezvous_url = str(hub_url).strip()
    if not token or not subnet_id or not rendezvous_url:
        typer.secho("[AdaOS] join failed: invalid response from server (missing token/subnet_id/hub_url)", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    cfg.token = token
    cfg.subnet_id = subnet_id
    cfg.hub_url = rendezvous_url
    try:
        cfg.root_settings.base_url = root.strip()
    except Exception:
        pass
    cfg.role = "member"
    save_config(cfg)

    out = {
        "ok": True,
        "node_id": cfg.node_id,
        "subnet_id": cfg.subnet_id,
        "role": cfg.role,
        "hub_url": cfg.hub_url,
        "root_url": cfg.root_settings.base_url,
        "join_url": used_url,
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
        sess = requests.Session()
        try:
            sess.trust_env = False
        except Exception:
            pass
        try:
            r = sess.get(url, headers=headers, timeout=2.5)
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
