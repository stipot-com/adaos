from __future__ import annotations

import json
from typing import Any

import typer

from adaos.services.agent_context import get_ctx
from adaos.services.join_codes import create as create_join_code

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
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
):
    """
    Create a short one-time join-code for adding member nodes to this hub's subnet.
    """
    ctx = get_ctx()
    conf = ctx.config
    if conf.role != "hub":
        raise typer.BadParameter("join-code creation is available only on a hub node (role=hub)")
    info = create_join_code(subnet_id=conf.subnet_id, ttl_seconds=int(ttl_minutes) * 60, length=int(length), ctx=ctx)
    out = {
        "ok": True,
        "code": info.code,
        "subnet_id": conf.subnet_id,
        "expires_at": info.expires_at,
    }
    _print(out, json_output=json_output)

