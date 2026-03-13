from __future__ import annotations

import asyncio

import typer

from adaos.services.realtime_sidecar import (
    apply_realtime_loop_policy,
    run_realtime_sidecar,
    realtime_sidecar_host,
    realtime_sidecar_port,
)

app = typer.Typer(help="Realtime sidecar (local NATS<->WS relay)")


@app.command("serve")
def serve(
    host: str = typer.Option(realtime_sidecar_host(), "--host"),
    port: int = typer.Option(realtime_sidecar_port(), "--port"),
) -> None:
    apply_realtime_loop_policy()
    raise SystemExit(asyncio.run(run_realtime_sidecar(host=host, port=port)))
