from __future__ import annotations

import argparse
import atexit
import os
import subprocess
import time
import traceback
from http import HTTPStatus
from pathlib import Path
from string import Formatter
from typing import Any
from urllib.parse import quote

import requests
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
from adaos.services.core_slots import active_slot, active_slot_manifest, rollback_to_previous_slot, slot_dir, slot_status
from adaos.services.node_config import load_config, save_config
from adaos.services.root.client import RootHttpClient
from adaos.services.root.core_update_sync import build_core_update_report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AdaOS API via autostart wrapper")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--token", default=None)
    return parser.parse_args()


def _resolved_token(raw_token: str | None = None) -> str | None:
    token = str(raw_token or os.getenv("ADAOS_TOKEN") or "").strip()
    if not token:
        try:
            conf = load_config()
        except Exception:
            conf = None
        token = str(getattr(conf, "token", "") or "").strip() if conf is not None else ""
    return token or None


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
        payload = build_core_update_report(conf)
        payload["status"] = status
        payload["reported_at"] = time.time()
        client.hub_core_update_report(payload=payload)
    except Exception:
        return


def _update_validation_timeout_sec() -> float:
    try:
        return max(1.0, float(os.getenv("ADAOS_CORE_UPDATE_VALIDATE_TIMEOUT_SEC") or "20"))
    except Exception:
        return 20.0


def _update_validation_strict() -> bool:
    raw = str(os.getenv("ADAOS_CORE_UPDATE_VALIDATE_STRICT") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _update_validation_runtime_guards_enabled() -> bool:
    raw = os.getenv("ADAOS_CORE_UPDATE_VALIDATE_RUNTIME")
    if raw is None:
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _update_validation_webspace_id() -> str:
    value = str(os.getenv("ADAOS_CORE_UPDATE_VALIDATE_WEBSPACE_ID") or "default").strip()
    return value or "default"


def _validation_headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["X-AdaOS-Token"] = str(token)
    return headers


def _required_update_checks(base_url: str) -> list[tuple[str, bool]]:
    if not _update_validation_strict():
        return [(f"{base_url}/api/ping", False)]
    return [
        (f"{base_url}/api/ping", False),
        (f"{base_url}/api/status", True),
        (f"{base_url}/api/admin/update/status", True),
    ]


def _validate_sidecar_runtime_payload(payload: dict[str, Any]) -> tuple[bool, str | None, dict[str, Any]]:
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    process = payload.get("process") if isinstance(payload.get("process"), dict) else {}
    enabled = bool(runtime.get("enabled"))
    status = str(runtime.get("status") or "").strip().lower() or ("enabled" if enabled else "disabled")
    listener_running = bool(process.get("listener_running") or str(runtime.get("local_listener_state") or "").strip().lower() == "ready")
    details = {
        "enabled": enabled,
        "status": status,
        "listener_running": listener_running,
        "transport_ready": bool(runtime.get("transport_ready")),
        "control_ready": runtime.get("control_ready"),
    }
    if not enabled:
        return True, None, details
    if listener_running:
        return True, None, details
    return False, "sidecar runtime is enabled but local listener is not running", details


def _validate_yjs_runtime_payload(
    payload: dict[str, Any],
    *,
    expected_webspace_id: str | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    assessment = runtime.get("assessment") if isinstance(runtime.get("assessment"), dict) else {}
    transport = runtime.get("transport") if isinstance(runtime.get("transport"), dict) else {}
    available = bool(runtime.get("available"))
    assessment_state = str(assessment.get("state") or "").strip().lower()
    selected_webspace_id = str(runtime.get("selected_webspace_id") or "").strip() or None
    server_ready = transport.get("server_ready")
    details = {
        "available": available,
        "assessment_state": assessment_state or None,
        "assessment_reason": assessment.get("reason"),
        "selected_webspace_id": selected_webspace_id,
        "server_ready": bool(server_ready) if isinstance(server_ready, bool) else None,
        "server_requested": bool(transport.get("server_requested")),
        "server_task_running": bool(transport.get("server_task_running")),
        "server_error": transport.get("server_error"),
    }
    if not available:
        reason = str(assessment.get("reason") or "Yjs runtime is unavailable").strip() or "Yjs runtime is unavailable"
        return False, reason, details
    if assessment_state == "unavailable":
        reason = str(assessment.get("reason") or "Yjs runtime is unavailable").strip() or "Yjs runtime is unavailable"
        return False, reason, details
    if isinstance(server_ready, bool) and not server_ready:
        reason = str(transport.get("server_error") or assessment.get("reason") or "Yjs websocket server is not ready").strip()
        return False, reason or "Yjs websocket server is not ready", details
    if expected_webspace_id and selected_webspace_id and selected_webspace_id != expected_webspace_id:
        return (
            False,
            f"Yjs runtime selected_webspace_id={selected_webspace_id}, expected {expected_webspace_id}",
            details,
        )
    return True, None, details


def _runtime_update_checks(base_url: str) -> list[tuple[str, bool, str, Any]]:
    if not _update_validation_runtime_guards_enabled():
        return []
    webspace_id = _update_validation_webspace_id()
    return [
        (
            f"{base_url}/api/node/sidecar/status",
            True,
            "sidecar_runtime",
            _validate_sidecar_runtime_payload,
        ),
        (
            f"{base_url}/api/node/yjs/webspaces/{quote(webspace_id, safe='')}/runtime",
            True,
            "yjs_runtime",
            lambda payload: _validate_yjs_runtime_payload(payload, expected_webspace_id=webspace_id),
        ),
    ]


def _tail_text(path: Path, *, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return text[-limit:].strip()


def _validation_log_paths(slot: str | None) -> tuple[Path, Path]:
    base_dir = Path(os.getenv("ADAOS_BASE_DIR") or (Path.home() / ".adaos")).expanduser().resolve()
    logs_dir = (base_dir / "logs").resolve()
    logs_dir.mkdir(parents=True, exist_ok=True)
    suffix = str(slot or "unknown").strip().upper() or "UNKNOWN"
    return (
        (logs_dir / f"autostart-slot-{suffix}.out.log").resolve(),
        (logs_dir / f"autostart-slot-{suffix}.err.log").resolve(),
    )


def _probe_update_runtime(
    *,
    host: str,
    port: int,
    token: str | None,
    timeout_sec: float,
    expected_slot: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    base_url = f"http://{host}:{int(port)}"
    deadline = time.time() + max(1.0, timeout_sec)
    last_error = "runtime validation did not start"
    headers = _validation_headers(token)
    strict = _update_validation_strict()
    attempts = 0
    last_attempt: dict[str, Any] = {}
    while time.time() < deadline:
        attempts += 1
        attempt: dict[str, Any] = {
            "ts": time.time(),
            "checks": [],
            "warnings": [],
        }
        for url, need_token in _required_update_checks(base_url):
            check: dict[str, Any] = {
                "url": url,
                "requires_token": bool(need_token),
            }
            try:
                response = requests.get(
                    url,
                    headers=headers if need_token else {"Accept": "application/json"},
                    timeout=1.5,
                )
                check["status_code"] = int(response.status_code)
                if response.status_code != HTTPStatus.OK:
                    last_error = f"{url} returned {response.status_code}"
                    check["ok"] = False
                    check["error"] = last_error
                    attempt["checks"].append(check)
                    break
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("ok") is False:
                    last_error = f"{url} returned invalid payload"
                    check["ok"] = False
                    check["error"] = last_error
                    attempt["checks"].append(check)
                    break
                if expected_slot and url.endswith("/api/admin/update/status"):
                    slots = payload.get("slots") if isinstance(payload.get("slots"), dict) else {}
                    active_manifest = (
                        payload.get("active_manifest") if isinstance(payload.get("active_manifest"), dict) else {}
                    )
                    active_slot_name = str(slots.get("active_slot") or active_manifest.get("slot") or "").strip().upper()
                    check["active_slot"] = active_slot_name
                    if active_slot_name and active_slot_name != str(expected_slot).strip().upper():
                        last_error = (
                            f"{url} returned active_slot={active_slot_name}, expected {str(expected_slot).strip().upper()}"
                        )
                        check["ok"] = False
                        check["error"] = last_error
                        attempt["checks"].append(check)
                        break
                check["ok"] = True
                check["payload_keys"] = sorted(str(key) for key in payload.keys())
                attempt["checks"].append(check)
            except Exception as exc:
                last_error = f"{url} probe failed: {exc}"
                check["ok"] = False
                check["error"] = str(exc)
                attempt["checks"].append(check)
                break
        else:
            if not strict:
                for url, need_token in [
                    (f"{base_url}/api/status", True),
                    (f"{base_url}/api/admin/update/status", True),
                ]:
                    check = {
                        "url": url,
                        "requires_token": bool(need_token),
                        "advisory": True,
                    }
                    try:
                        response = requests.get(
                            url,
                            headers=headers if need_token else {"Accept": "application/json"},
                            timeout=1.5,
                        )
                        check["status_code"] = int(response.status_code)
                        if response.status_code != HTTPStatus.OK:
                            check["ok"] = False
                            check["error"] = f"{url} returned {response.status_code}"
                            attempt["warnings"].append(dict(check))
                            attempt["checks"].append(check)
                            continue
                        payload = response.json()
                        if not isinstance(payload, dict) or payload.get("ok") is False:
                            check["ok"] = False
                            check["error"] = f"{url} returned invalid payload"
                            attempt["warnings"].append(dict(check))
                            attempt["checks"].append(check)
                            continue
                        if expected_slot and url.endswith("/api/admin/update/status"):
                            slots = payload.get("slots") if isinstance(payload.get("slots"), dict) else {}
                            active_manifest = (
                                payload.get("active_manifest") if isinstance(payload.get("active_manifest"), dict) else {}
                            )
                            active_slot_name = str(slots.get("active_slot") or active_manifest.get("slot") or "").strip().upper()
                            check["active_slot"] = active_slot_name
                            if active_slot_name and active_slot_name != str(expected_slot).strip().upper():
                                last_error = (
                                    f"{url} returned active_slot={active_slot_name}, expected {str(expected_slot).strip().upper()}"
                                )
                                check["ok"] = False
                                check["error"] = last_error
                                attempt["checks"].append(check)
                                break
                        check["ok"] = True
                        check["payload_keys"] = sorted(str(key) for key in payload.keys())
                        attempt["checks"].append(check)
                    except Exception as exc:
                        check["ok"] = False
                        check["error"] = str(exc)
                        attempt["warnings"].append(dict(check))
                        attempt["checks"].append(check)
                else:
                    runtime_checks = _runtime_update_checks(base_url)
                    runtime_failed = False
                    for runtime_url, runtime_need_token, runtime_check_id, runtime_validator in runtime_checks:
                        check = {
                            "url": runtime_url,
                            "requires_token": bool(runtime_need_token),
                            "check_id": runtime_check_id,
                        }
                        try:
                            response = requests.get(
                                runtime_url,
                                headers=headers if runtime_need_token else {"Accept": "application/json"},
                                timeout=1.5,
                            )
                            check["status_code"] = int(response.status_code)
                            if response.status_code != HTTPStatus.OK:
                                last_error = f"{runtime_url} returned {response.status_code}"
                                check["ok"] = False
                                check["error"] = last_error
                                attempt["checks"].append(check)
                                runtime_failed = True
                                break
                            payload = response.json()
                            if not isinstance(payload, dict) or payload.get("ok") is False:
                                last_error = f"{runtime_url} returned invalid payload"
                                check["ok"] = False
                                check["error"] = last_error
                                attempt["checks"].append(check)
                                runtime_failed = True
                                break
                            valid, error_text, details = runtime_validator(payload)
                            check["ok"] = bool(valid)
                            check["payload_keys"] = sorted(str(key) for key in payload.keys())
                            if details:
                                check["details"] = details
                            if not valid:
                                last_error = str(error_text or f"{runtime_url} failed runtime validation")
                                check["error"] = last_error
                                attempt["checks"].append(check)
                                runtime_failed = True
                                break
                            attempt["checks"].append(check)
                        except Exception as exc:
                            last_error = f"{runtime_url} probe failed: {exc}"
                            check["ok"] = False
                            check["error"] = str(exc)
                            attempt["checks"].append(check)
                            runtime_failed = True
                            break
                    if runtime_failed:
                        last_attempt = attempt
                        time.sleep(0.5)
                        continue
                    return True, {
                        "ok": True,
                        "summary": "ok" if not attempt["warnings"] else "ok_with_warnings",
                        "base_url": base_url,
                        "attempts": attempts,
                        "token_present": bool(token),
                        "strict": strict,
                        "runtime_guards": bool(runtime_checks),
                        "last_attempt": attempt,
                    }
                last_attempt = attempt
                time.sleep(0.5)
                continue
            return True, {
                "ok": True,
                "summary": "ok",
                "base_url": base_url,
                "attempts": attempts,
                "token_present": bool(token),
                "strict": strict,
                "runtime_guards": bool(_runtime_update_checks(base_url)),
                "last_attempt": attempt,
            }
        last_attempt = attempt
        time.sleep(0.5)
    return False, {
        "ok": False,
        "summary": last_error,
        "base_url": base_url,
        "attempts": attempts,
        "token_present": bool(token),
        "strict": strict,
        "runtime_guards": bool(_runtime_update_checks(base_url)),
        "last_attempt": last_attempt,
    }


def _launch_active_slot_if_needed(args: argparse.Namespace, *, host: str, port: int, validate: bool = False) -> None:
    slot = active_slot()
    if not slot:
        return
    if str(os.getenv("ADAOS_ACTIVE_CORE_SLOT") or "").strip().upper() == slot:
        return
    manifest = active_slot_manifest()
    if not isinstance(manifest, dict):
        return
    resolved_token = _resolved_token(args.token)
    argv, command = _slot_launch_spec(manifest, host=host, port=port, token=resolved_token)
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
    if resolved_token:
        env["ADAOS_TOKEN"] = str(resolved_token)
    cwd_raw = str(manifest.get("cwd") or "").strip()
    cwd = Path(cwd_raw).expanduser().resolve() if cwd_raw else None
    if validate:
        started_at = time.time()
        stdout_path, stderr_path = _validation_log_paths(slot)
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        with stdout_path.open("a", encoding="utf-8", buffering=1) as stdout_fh, stderr_path.open(
            "a", encoding="utf-8", buffering=1
        ) as stderr_fh:
            proc = subprocess.Popen(
                argv or command or [],
                shell=bool(command),
                env=env,
                cwd=str(cwd) if cwd else None,
                stdout=stdout_fh,
                stderr=stderr_fh,
            )
            ok, details = _probe_update_runtime(
                host=host,
                port=port,
                token=resolved_token,
                timeout_sec=_update_validation_timeout_sec(),
                expected_slot=slot,
            )
            if ok:
                write_status(
                    {
                        "state": "succeeded",
                        "phase": "validate",
                        "message": f"slot {slot} passed post-switch validation",
                        "target_slot": slot,
                        "manifest": manifest,
                        "validated_at": time.time(),
                        "validation_logs": {
                            "stdout_path": str(stdout_path),
                            "stderr_path": str(stderr_path),
                        },
                    }
                )
                raise SystemExit(int(proc.wait()))
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except Exception:
                proc.kill()
        validation_stdout = _tail_text(stdout_path)
        validation_stderr = _tail_text(stderr_path)
        restored = rollback_to_previous_slot()
        payload: dict[str, Any] = {
            "state": "failed",
            "phase": "validate",
            "message": f"slot {slot} failed post-switch validation",
            "validation_error": details,
            "validation_error_summary": str(details.get("summary") or "validation failed")
            if isinstance(details, dict)
            else str(details or "validation failed"),
            "validation_stdout": validation_stdout,
            "validation_stderr": validation_stderr,
            "validation_logs": {
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            },
            "target_slot": slot,
            "manifest": manifest,
            "started_at": started_at,
            "finished_at": time.time(),
            "restored_slot": restored or "",
        }
        if restored:
            payload["rollback"] = {"ok": True, "slot": restored}
        write_status(payload)
        raise SystemExit(1)
    completed = subprocess.run(argv or command or [], shell=bool(command), env=env, cwd=str(cwd) if cwd else None)
    raise SystemExit(int(completed.returncode))


def main() -> None:
    args = _parse_args()
    token = _resolved_token(args.token)
    phase = "init"
    pidfile: Path | None = None
    try:
        phase = "init_ctx"
        init_ctx()
        phase = "read_plan"
        plan = read_plan()
        pending_update_succeeded = False
        conf = None
        try:
            conf = load_config()
        except Exception:
            conf = None
        if plan:
            phase = "execute_pending_update"
            result = execute_pending_update(plan)
            clear_plan()
            if conf is not None:
                _upload_update_report(result, conf)
            if str(result.get("state") or "") != "succeeded":
                raise SystemExit(int(result.get("returncode") or 1) or 1)
            pending_update_succeeded = True
        else:
            phase = "boot"
            write_status({"state": "idle", "message": "autostart runner boot", "updated_at": time.time()})

        phase = "resolve_bind"
        host, port = _resolve_bind(conf, args.host, args.port)
        advertised_base = _advertise_base(host, port)
        phase = "stop_previous_server"
        _stop_previous_server(host, port)
        phase = "write_pidfile"
        pidfile = _pidfile_path(host, port)
        _write_pidfile(pidfile, host=host, port=port, advertised_base=advertised_base)
        atexit.register(_cleanup_pidfile, pidfile)

        if conf is not None and str(getattr(conf, "role", "") or "").strip().lower() == "hub":
            phase = "update_hub_url"
            try:
                if str(getattr(conf, "hub_url", "") or "").strip() != advertised_base:
                    conf.hub_url = advertised_base
                    save_config(conf)
            except Exception:
                pass

        if token:
            os.environ["ADAOS_TOKEN"] = str(token)
        os.environ["ADAOS_SELF_BASE_URL"] = advertised_base
        os.environ["ADAOS_AUTOSTART_MODE"] = "1"

        phase = "launch_active_slot"
        _launch_active_slot_if_needed(args, host=host, port=port, validate=pending_update_succeeded)

        from adaos.apps.api.server import app as server_app

        phase = "uvicorn.run"
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
            if pidfile is not None:
                _cleanup_pidfile(pidfile)
    except SystemExit:
        raise
    except Exception as exc:
        try:
            write_status(
                {
                    "state": "failed",
                    "phase": phase,
                    "message": f"autostart runner failed during {phase}",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=20),
                    "updated_at": time.time(),
                }
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
