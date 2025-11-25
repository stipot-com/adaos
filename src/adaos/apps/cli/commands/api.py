# src\adaos\apps\cli\commands\api.py
import os
from urllib.parse import urlparse

import typer
import uvicorn

from adaos.apps.cli.i18n import _
from adaos.services.node_config import load_config, save_config

app = typer.Typer(help="HTTP API ��� AdaOS")


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8777, "--port"),
    reload: bool = typer.Option(False, "--reload", help="��� ࠧࠡ�⪨"),
    token: str = typer.Option(None, "--token", help="X-AdaOS-Token; ���� ���쬥� �� ADAOS_TOKEN"),
):
    """�������� HTTP API (FastAPI)."""
    # 1) Попробуем взять базовый адрес из node.yaml (hub_url).
    #    Если он есть, и host/port не переопределены, используем его.
    try:
        conf = load_config()
    except Exception:
        conf = None

    if conf is not None and conf.hub_url:
        if host == "127.0.0.1" and port == 8777:
            try:
                u = urlparse(conf.hub_url)
                if u.hostname and u.port:
                    host = u.hostname
                    port = u.port
            except Exception:
                pass
    elif conf is not None and not conf.hub_url:
        # Если hub_url не задан, фиксируем его как текущий слушающий адрес.
        try:
            conf.hub_url = f"http://{host}:{port}"
            save_config(conf)
        except Exception:
            pass

    if token:
        os.environ["ADAOS_TOKEN"] = token
    # Advertise this node's base URL for subnet register/heartbeat
    try:
        os.environ["ADAOS_SELF_BASE_URL"] = f"http://{host}:{port}"
    except Exception:
        pass
    # �窠 �室� FastAPI
    uvicorn.run("adaos.apps.api.server:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()

