from __future__ import annotations

import json
import os
from typing import Any

import requests
import typer

from adaos.services.agent_context import get_ctx
from adaos.services.join_codes import create as create_join_code
from adaos.services.root.client import RootHttpClient
from adaos.services.root.service import RootAuthError, RootAuthService

app = typer.Typer(help="Hub operations.")
join_code_app = typer.Typer(help="Join-code management.")
app.add_typer(join_code_app, name="join-code")


def _print(data: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, dict) and "code" in data:
            typer.echo(str(data["code"]))
        else:
            typer.echo(str(data))


@join_code_app.command("create")
def join_code_create(
    ttl_minutes: int = typer.Option(15, "--ttl-min", min=1, max=60),
    length: int = typer.Option(8, "--length", min=8, max=12),
    root: str | None = typer.Option(None, "--root", help="Root server base URL (default: node.yaml root.base_url)"),
    hub_url: str | None = typer.Option(None, "--hub-url", help="Hub base URL reachable by members (default: $ADAOS_SELF_BASE_URL)"),
    local: bool = typer.Option(False, "--local", help="Create join-code locally on the hub (offline mode; member must join via hub URL)"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    """
    Create a short one-time join-code for adding member nodes to this hub's subnet.

    Default mode: create a Root session so members can join via Root using the short code.
    Offline mode: use `--local` to create the join-code on the hub directly.
    """
    ctx = get_ctx()
    conf = ctx.config
    if conf.role != "hub":
        raise typer.BadParameter("join-code creation is available only on a hub node (role=hub)")

    if local:
        info = create_join_code(subnet_id=conf.subnet_id, ttl_seconds=int(ttl_minutes) * 60, length=int(length), ctx=ctx)
        out = {
            "ok": True,
            "mode": "local",
            "code": info.code,
            "subnet_id": conf.subnet_id,
            "expires_at": info.expires_at,
        }
        _print(out, json_output=json_output)
        return

    root_base = (root or getattr(getattr(conf, "root_settings", None), "base_url", None) or "https://api.inimatic.com").rstrip("/")
    effective_hub_url = (hub_url or os.getenv("ADAOS_SELF_BASE_URL") or "").strip()
    if not effective_hub_url:
        raise typer.BadParameter("hub_url is required (pass --hub-url or set ADAOS_SELF_BASE_URL)")

    # Get Root access token from cached/auto-refreshed owner session.
    try:
        auth = RootAuthService(http=RootHttpClient(base_url=root_base))
        access_token = auth.get_access_token(conf)
    except RootAuthError as exc:
        raise typer.BadParameter(f"Root session is not configured: {exc}. Run: adaos dev root login") from exc

    url = root_base + "/v1/subnets/join-code"
    payload = {
        "subnet_id": conf.subnet_id,
        "hub_url": effective_hub_url,
        "token": conf.token or "dev-local-token",
        "ttl_minutes": int(ttl_minutes),
        "length": int(length),
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    sess = requests.Session()
    try:
        sess.trust_env = False
    except Exception:
        pass
    try:
        resp = sess.post(url, json=payload, headers=headers, timeout=10)
    except Exception as exc:
        raise typer.BadParameter(f"Root join-code create failed: {type(exc).__name__}: {exc}") from exc

    if resp.status_code != 200:
        try:
            body = resp.json()
        except Exception:
            body = (resp.text or "").strip()
        raise typer.BadParameter(f"Root join-code create failed: HTTP {resp.status_code}; url={url}; body={body}")

    data = resp.json() or {}
    code = str(data.get("code") or "").strip()
    expires_at_utc = data.get("expires_at_utc")
    if not code:
        raise typer.BadParameter("Root join-code create returned invalid response (missing code)")

    out = {
        "ok": True,
        "mode": "root",
        "code": code,
        "subnet_id": conf.subnet_id,
        "root_url": root_base,
        "hub_url": effective_hub_url,
        "expires_at_utc": expires_at_utc,
    }
    _print(out, json_output=json_output)
