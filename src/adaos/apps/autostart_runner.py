from __future__ import annotations

import argparse
import atexit
import os
import subprocess
import time
from pathlib import Path
from string import Formatter

import uvicorn

from adaos.apps.bootstrap import init_ctx
from adaos.apps.cli.commands.api import (
    _advertise_base,
    _cleanup_pidfile,
    _pidfile_path,
    _resolve_bind,
    _stop_previous_server,
    _uvicorn_loop_mode,
    _write_pidfile,
)
from adaos.services.agent_context import get_ctx
from adaos.services.core_update import clear_plan, execute_pending_update, read_plan, write_status
from adaos.services.core_slots import active_slot, active_slot_manifest, slot_dir, slot_status
from adaos.services.node_config import load_config, save_config
from adaos.services.root.client import RootHttpClient


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AdaOS API via autostart wrapper")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--token", default=None)
    return parser.parse_args()


def _format_slot_value(template: str, values: dict[str, str]) -> str:
    fields = {field_name for _, field_name, _, _ in Formatter().parse(template) if field_name}
    payload = dict(values)
    for field in fields:
        payload.setdefault(field, "")
    return template.format(**payload)


def _slot_launch_spec(manifest: dict[str, object], *, host: str, port: int, token: str | None) -> tuple[list[str] | None, str | None]:
    slot = str(manifest.get("slot") or "").strip().upper()
    try:
        ctx = get_ctx()
        base_dir = ctx.paths.base_dir()
        base_dir = base_dir() if callable(base_dir) else base_dir
        base_dir_text = str(Path(base_dir).expanduser().resolve())
    except Exception:
        base_dir_text = str(os.getenv("ADAOS_BASE_DIR") or "")
    values = {
        "host": str(host),
        "port": str(port),
        "token": str(token or ""),
        "slot": slot,
        "slot_dir": str(slot_dir(slot)) if slot else "",
        "base_dir": base_dir_text,
        "python": os.sys.executable,
    }
    argv_raw = manifest.get("argv")
    if isinstance(argv_raw, list):
        argv = [_format_slot_value(str(item), values) for item in argv_raw if str(item).strip()]
        if argv:
            return argv, None
    command = str(manifest.get("command") or "").strip()
    if command:
        return None, _format_slot_value(command, values)
    return None, None


def _upload_update_report(status: dict[str, object], conf) -> None:
    try:
        base_url = str(getattr(getattr(conf, "root_settings", None), "base_url", None) or "").rstrip("/")
        cert_path = conf.hub_cert_path()
        key_path = conf.hub_key_path()
        ca_path = conf.ca_cert_path()
        if not base_url or not cert_path.exists() or not key_path.exists():
            return
        verify: str | bool = str(ca_path) if ca_path.exists() else True
        client = RootHttpClient(base_url=base_url, verify=verify, cert=(str(cert_path), str(key_path)))
        client.hub_core_update_report(
            payload={
                "status": status,
                "slot_status": slot_status(),
                "node_id": str(getattr(conf, "node_id", "") or ""),
                "subnet_id": str(getattr(conf, "subnet_id", "") or ""),
                "role": str(getattr(conf, "role", "") or ""),
                "reported_at": time.time(),
            },
        )
    except Exception:
        return


def _launch_active_slot_if_needed(args: argparse.Namespace, *, host: str, port: int) -> None:
    slot = active_slot()
    if not slot:
        return
    if str(os.getenv("ADAOS_ACTIVE_CORE_SLOT") or "").strip().upper() == slot:
        return
    manifest = active_slot_manifest()
    if not isinstance(manifest, dict):
        return
    argv, command = _slot_launch_spec(manifest, host=host, port=port, token=args.token)
    if not argv and not command:
        write_status(
            {
                "state": "failed",
                "phase": "launch",
                "message": f"active slot {slot} manifest has no launch command",
                "slot_status": slot_status(),
            }
        )
        raise SystemExit(2)
    env = dict(os.environ)
    manifest_env = manifest.get("env")
    if isinstance(manifest_env, dict):
        for key, value in manifest_env.items():
            env[str(key)] = str(value)
    env["ADAOS_ACTIVE_CORE_SLOT"] = slot
    env["ADAOS_ACTIVE_CORE_SLOT_DIR"] = str(slot_dir(slot))
    if args.token:
        env["ADAOS_TOKEN"] = str(args.token)
    cwd_raw = str(manifest.get("cwd") or "").strip()
    cwd = Path(cwd_raw).expanduser().resolve() if cwd_raw else None
    completed = subprocess.run(argv or command or [], shell=bool(command), env=env, cwd=str(cwd) if cwd else None)
    raise SystemExit(int(completed.returncode))


def main() -> None:
    args = _parse_args()
    init_ctx()
    plan = read_plan()
    conf = None
    try:
        conf = load_config()
    except Exception:
        conf = None
    if plan:
        result = execute_pending_update(plan)
        clear_plan()
        if conf is not None:
            _upload_update_report(result, conf)
        if str(result.get("state") or "") != "succeeded":
            raise SystemExit(int(result.get("returncode") or 1) or 1)
    else:
        write_status({"state": "idle", "message": "autostart runner boot", "updated_at": time.time()})

    host, port = _resolve_bind(conf, args.host, args.port)
    advertised_base = _advertise_base(host, port)
    _stop_previous_server(host, port)
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

    _launch_active_slot_if_needed(args, host=host, port=port)

    from adaos.apps.api.server import app as server_app

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
