# src\adaos\apps\cli\commands\api.py
import os
from urllib.parse import urlparse

import typer
import uvicorn

from adaos.services.node_config import load_config, save_config

app = typer.Typer(help="HTTP API for AdaOS")


def _is_local_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _advertise_base(host: str, port: int) -> str:
    advertised_host = (host or "").strip() or "127.0.0.1"
    if advertised_host in {"0.0.0.0", "::", "[::]"}:
        advertised_host = "127.0.0.1"
    return f"http://{advertised_host}:{int(port)}"


def _resolve_bind(conf, host: str, port: int) -> tuple[str, int]:
    role = str(getattr(conf, "role", "") or "").strip().lower() if conf is not None else ""
    if role != "hub":
        return host, int(port)
    if host != "127.0.0.1" or int(port) != 8777:
        return host, int(port)
    hub_url = str(getattr(conf, "hub_url", "") or "").strip()
    if not _is_local_url(hub_url):
        return host, int(port)
    try:
        parsed = urlparse(hub_url)
        if parsed.hostname and parsed.port:
            return parsed.hostname, int(parsed.port)
    except Exception:
        pass
    return host, int(port)


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8777, "--port"),
    reload: bool = typer.Option(False, "--reload", help="Enable uvicorn autoreload"),
    token: str | None = typer.Option(None, "--token", help="Override X-AdaOS-Token / ADAOS_TOKEN"),
):
    """Serve the AdaOS local HTTP API."""
    conf = None
    try:
        conf = load_config()
    except Exception:
        conf = None

    host, port = _resolve_bind(conf, host, port)
    advertised_base = _advertise_base(host, port)

    if conf is not None and str(getattr(conf, "role", "") or "").strip().lower() == "hub":
        try:
            if str(getattr(conf, "hub_url", "") or "").strip() != advertised_base:
                conf.hub_url = advertised_base
                save_config(conf)
        except Exception:
            pass

    if token:
        os.environ["ADAOS_TOKEN"] = token
    try:
        os.environ["ADAOS_SELF_BASE_URL"] = advertised_base
    except Exception:
        pass

    uvicorn.run(
        "adaos.apps.api.server:app",
        host=host,
        port=int(port),
        reload=reload,
        access_log=False,
    )


if __name__ == "__main__":
    app()
