from __future__ import annotations

import argparse
import atexit
import os

import uvicorn

from adaos.apps.api.server import app as server_app
from adaos.apps.cli.commands.api import (
    _advertise_base,
    _cleanup_pidfile,
    _pidfile_path,
    _resolve_bind,
    _uvicorn_loop_mode,
    _write_pidfile,
)
from adaos.services.node_config import load_config, save_config


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AdaOS API via autostart wrapper")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--token", default=None)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    conf = None
    try:
        conf = load_config()
    except Exception:
        conf = None

    host, port = _resolve_bind(conf, args.host, args.port)
    advertised_base = _advertise_base(host, port)
    pidfile = _pidfile_path(host, port)

    _write_pidfile(pidfile, host=host, port=port, advertised_base=advertised_base)
    atexit.register(_cleanup_pidfile, pidfile)

    if conf is not None and str(getattr(conf, "role", "") or "").strip().lower() == "hub":
        try:
            if str(getattr(conf, "hub_url", "") or "").strip() != advertised_base:
                conf.hub_url = advertised_base
                save_config(conf)
        except Exception:
            pass

    if args.token:
        os.environ["ADAOS_TOKEN"] = str(args.token)
    os.environ["ADAOS_SELF_BASE_URL"] = advertised_base
    os.environ["ADAOS_AUTOSTART_MODE"] = "1"

    try:
        uvicorn.run(
            server_app,
            host=host,
            port=int(port),
            loop=_uvicorn_loop_mode(),
            reload=False,
            workers=1,
            access_log=False,
        )
    finally:
        _cleanup_pidfile(pidfile)


if __name__ == "__main__":
    main()
