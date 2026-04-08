from __future__ import annotations

import asyncio
import json
import os
import time
import subprocess
import traceback
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

import psutil
import requests
import typer
from requests import RequestException

from adaos.adapters.db import SqliteScenarioRegistry, SqliteSkillRegistry
from adaos.apps.cli.i18n import _
from adaos.apps.cli.active_control import probe_control_api, resolve_control_base_url, resolve_control_token
from adaos.apps.cli.commands.api import (
    _current_process_family_pids,
    _find_listening_server_pid,
    _find_matching_server_pids,
    _pidfile_path,
    _read_pidfile,
    _resolve_stop_bind,
)
from adaos.build_info import BUILD_INFO
from adaos.services.agent_context import get_ctx
from adaos.services.autostart import default_spec as default_autostart_spec
from adaos.services.autostart import disable as autostart_disable
from adaos.services.autostart import enable as autostart_enable
from adaos.services.autostart import status as autostart_status
from adaos.services.core_slots import active_slot_manifest, slot_status as core_slot_status
from adaos.services.core_update import last_result_path as core_update_last_result_path
from adaos.services.core_update import plan_path as core_update_plan_path
from adaos.services.core_update import status_path as core_update_status_path
from adaos.services.scenario.manager import ScenarioManager
from adaos.services.scenario.webspace_runtime import rebuild_webspace_from_sources
from adaos.services.setup.presets import get_preset
from adaos.services.skill.manager import SkillManager
from adaos.services.yjs.bootstrap import ensure_webspace_seeded_from_scenario
from adaos.services.yjs.store import get_ystore_for_webspace
from adaos.services.yjs.webspace import default_webspace_id
from adaos.services.workspace_sync import installed_names as _workspace_sync_installed_names
from adaos.services.workspace_sync import sync_workspace_sparse_to_registry as _workspace_sync_sparse_to_registry
from adaos.services.workspace_sync import workspace_kind_names as _workspace_sync_kind_names


def _run_safe(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            if os.getenv("ADAOS_CLI_DEBUG") == "1":
                traceback.print_exc()
            raise

    return wrapper


def _scenario_mgr() -> ScenarioManager:
    ctx = get_ctx()
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=ctx.scenarios_repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


def _skill_mgr() -> SkillManager:
    ctx = get_ctx()
    reg = SqliteSkillRegistry(ctx.sql)
    return SkillManager(repo=ctx.skills_repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)

def _installed_names(rows: list[object]) -> list[str]:
    return _workspace_sync_installed_names(rows)


def _workspace_kind_names(ctx, workspace_root, kind: str) -> list[str]:
    return _workspace_sync_kind_names(ctx, workspace_root, kind)


def _effective_registry_names(ctx, registry_names: list[str], workspace_root, kind: str) -> tuple[list[str], bool]:
    names = sorted(set(str(name) for name in (registry_names or []) if str(name).strip()))
    if names:
        return names, False
    fallback = _workspace_kind_names(ctx, workspace_root, kind)
    if fallback:
        return fallback, True
    return [], False


def _sync_workspace_sparse_to_registry(ctx) -> dict:
    return _workspace_sync_sparse_to_registry(ctx)


@_run_safe
def install(
    preset: str = typer.Option("default", "--preset", help="default | base"),
    webspace_id: Optional[str] = typer.Option(None, "--webspace", help="target webspace id (default: 'default')"),
    setup_skills: bool = typer.Option(False, "--setup", help="run skill setup hooks (may prompt / require IO)"),
    autostart: bool = typer.Option(False, "--autostart", help="enable OS autostart after install"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
) -> None:
    """
    Install default scenarios/skills into the local workspace.

    Assumes the runtime environment is already bootstrapped (e.g. via tools/bootstrap_uv.ps1).
    """
    ctx = get_ctx()
    target_webspace = webspace_id or default_webspace_id()
    chosen = get_preset(preset)

    scenario_mgr = _scenario_mgr()
    skill_mgr = _skill_mgr()

    installed = {"scenarios": [], "skills": [], "warnings": []}

    for scenario_id in chosen.scenarios:
        try:
            meta = scenario_mgr.install_with_deps(scenario_id, webspace_id=target_webspace)
            installed["scenarios"].append({"id": meta.id.value, "version": getattr(meta, "version", None)})
        except Exception as exc:
            installed["warnings"].append(f"scenario {scenario_id}: {exc}")

    for skill_id in chosen.skills:
        try:
            skill_mgr.install(skill_id, validate=False)
            runtime = None
            try:
                runtime = skill_mgr.prepare_runtime(skill_id, run_tests=False)
            except Exception:
                runtime = None
            version = getattr(runtime, "version", None) if runtime else None
            slot = getattr(runtime, "slot", None) if runtime else None
            skill_mgr.activate_for_space(skill_id, version=version, slot=slot, space="default", webspace_id=target_webspace)
            if setup_skills:
                try:
                    skill_mgr.setup_skill(skill_id)
                except Exception as exc:
                    installed["warnings"].append(f"skill setup {skill_id}: {exc}")
            installed["skills"].append({"id": skill_id, "version": version, "slot": slot})
        except Exception as exc:
            installed["warnings"].append(f"skill {skill_id}: {exc}")

    # Persist native IO availability (voice/TTS) into node.yaml capacity so the
    # system can avoid offering non-working IO paths on servers.
    try:
        from adaos.services.capacity import refresh_native_io_capacity

        refresh_native_io_capacity()
    except Exception as exc:
        installed["warnings"].append(f"capacity refresh: {exc}")

    # Ensure the webspace has at least some UI/application seeded.
    try:
        default_scenario = chosen.scenarios[0] if chosen.scenarios else "web_desktop"
        ystore = get_ystore_for_webspace(target_webspace)
        asyncio.run(ensure_webspace_seeded_from_scenario(ystore, target_webspace, default_scenario_id=default_scenario))
    except Exception as exc:
        installed["warnings"].append(f"webspace seed: {exc}")
    # CLI does not necessarily have runtime event subscribers loaded; rebuild
    # effective UI explicitly so ui.application/data.catalog are populated.
    try:
        asyncio.run(
            rebuild_webspace_from_sources(
                target_webspace,
                action="cli_setup_install_sync",
                source_of_truth="scenario_projection",
            )
        )
    except Exception as exc:
        installed["warnings"].append(f"webspace rebuild: {exc}")

    if autostart:
        try:
            spec = default_autostart_spec(ctx)
            res = autostart_enable(ctx, spec)
            installed["autostart"] = res
        except Exception as exc:
            installed["warnings"].append(f"autostart enable: {exc}")

    if json_output:
        typer.echo(json.dumps(installed, ensure_ascii=False, indent=2))
        return

    typer.secho(f"[AdaOS] installed preset '{chosen.name}' into webspace '{target_webspace}'", fg=typer.colors.GREEN)
    for item in installed["scenarios"]:
        typer.echo(f"scenario: {item['id']} ({item.get('version') or 'unknown'})")
    for item in installed["skills"]:
        slot = item.get("slot") or "n/a"
        typer.echo(f"skill: {item['id']} (slot {slot})")
    if installed["warnings"]:
        typer.secho("warnings:", fg=typer.colors.YELLOW)
        for w in installed["warnings"]:
            typer.echo(f"  - {w}")


@_run_safe
def update(
    pull: bool = typer.Option(True, "--pull/--no-pull", help="pull latest scenario/skill sources from git"),
    sync_yjs: bool = typer.Option(True, "--sync-yjs/--no-sync-yjs", help="re-project installed scenarios into Yjs webspace"),
    migrate_runtime: bool = typer.Option(
        True,
        "--migrate-runtime/--no-migrate-runtime",
        help="ensure missing/broken skill runtimes are prepared+activated after update",
    ),
    webspace_id: Optional[str] = typer.Option(None, "--webspace", help="target webspace id (default: 'default')"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
) -> None:
    """Update installed scenarios/skills and refresh runtime slots from workspace sources."""
    ctx = get_ctx()
    target_webspace = webspace_id or default_webspace_id()
    scenario_mgr = _scenario_mgr()
    skill_mgr = _skill_mgr()
    out: dict = {"pulled": {}, "runtime_updated": [], "yjs_synced": [], "warnings": []}
    effective_scenario_names: list[str] = []

    if pull:
        try:
            # Apply sparse-checkout union once, then pull once.
            res = _sync_workspace_sparse_to_registry(ctx)
            effective_scenario_names = [str(name) for name in (res.get("scenarios") or []) if str(name).strip()]
            out["pulled"]["workspace"] = bool(res.get("ok"))
            out["pulled"]["skills"] = bool(res.get("ok"))
            out["pulled"]["scenarios"] = bool(res.get("ok"))
            fallback_used = res.get("fallback_used") or {}
            if isinstance(fallback_used, dict):
                if fallback_used.get("skills"):
                    out["warnings"].append("skill registry empty; preserved workspace skills from current sparse/materialized state")
                if fallback_used.get("scenarios"):
                    out["warnings"].append("scenario registry empty; preserved workspace scenarios from current sparse/materialized state")
            if not res.get("ok"):
                out["warnings"].append(f"workspace pull: {res.get('error')}")
        except Exception as exc:
            out["warnings"].append(f"workspace pull: {exc}")
            out["pulled"]["workspace"] = False
            out["pulled"]["skills"] = False
            out["pulled"]["scenarios"] = False

    # Refresh runtime slots (keeps slot/version, syncs files + tools).
    try:
        skill_rows = SqliteSkillRegistry(ctx.sql).list()
    except Exception:
        skill_rows = []
    for row in skill_rows:
        name = getattr(row, "name", None) or getattr(row, "id", None)
        if not name or not bool(getattr(row, "installed", True)):
            continue
        try:
            runtime_status_before = {}
            try:
                runtime_status_before = skill_mgr.runtime_status(str(name))
            except Exception:
                runtime_status_before = {}
            runtime_version_before = str(runtime_status_before.get("version") or "").strip()
            res = skill_mgr.runtime_update(str(name), space="workspace")
            entry = {"skill": str(name), "ok": True, "result": res}
            try:
                source_meta = ctx.skills_repo.get(str(name))
            except Exception:
                source_meta = None
            source_version = str(getattr(source_meta, "version", None) or "").strip()
            should_prepare = bool(source_version and source_version != runtime_version_before)
            if isinstance(res, dict) and not bool(res.get("ok", True)):
                should_prepare = True

            # If runtime is missing/unprepared, optionally rebuild it using the
            # full prepare+activate flow (also refreshes node.yaml capacity).
            if migrate_runtime and should_prepare:
                try:
                    skill_mgr.install(str(name), validate=False)
                    runtime = skill_mgr.prepare_runtime(str(name), run_tests=False)
                    version = getattr(runtime, "version", None)
                    slot = getattr(runtime, "slot", None)
                    skill_mgr.activate_for_space(
                        str(name),
                        version=version,
                        slot=slot,
                        space="default",
                        webspace_id=target_webspace,
                    )
                    entry["runtime_migrated"] = True
                    entry["migrated_version"] = version
                    entry["migrated_slot"] = slot
                except Exception as exc:
                    entry["runtime_migrated"] = False
                    entry["migration_error"] = str(exc)

            out["runtime_updated"].append(entry)
        except Exception as exc:
            entry = {"skill": str(name), "ok": False, "error": str(exc)}
            if migrate_runtime and "no versions installed" in str(exc).lower():
                try:
                    skill_mgr.install(str(name), validate=False)
                    runtime = skill_mgr.prepare_runtime(str(name), run_tests=False)
                    version = getattr(runtime, "version", None)
                    slot = getattr(runtime, "slot", None)
                    skill_mgr.activate_for_space(
                        str(name),
                        version=version,
                        slot=slot,
                        space="default",
                        webspace_id=target_webspace,
                    )
                    entry["runtime_migrated"] = True
                    entry["migrated_version"] = version
                    entry["migrated_slot"] = slot
                except Exception as mig_exc:
                    entry["runtime_migrated"] = False
                    entry["migration_error"] = str(mig_exc)
            out["runtime_updated"].append(entry)

    if sync_yjs:
        try:
            scenario_rows = SqliteScenarioRegistry(ctx.sql).list()
        except Exception:
            scenario_rows = []
        scenario_names = _installed_names(scenario_rows)
        if not scenario_names and effective_scenario_names:
            scenario_names = list(effective_scenario_names)
            out["warnings"].append("scenario registry empty during YJS sync; used workspace fallback scenario list")
        if not scenario_names:
            workspace_fallback_scenarios = _workspace_kind_names(ctx, ctx.paths.workspace_dir(), "scenarios")
            if workspace_fallback_scenarios:
                scenario_names = workspace_fallback_scenarios
                out["warnings"].append("scenario registry empty during YJS sync; discovered scenarios from current workspace")
        for name in scenario_names:
            try:
                scenario_mgr.sync_to_yjs(str(name), webspace_id=target_webspace)
                out["yjs_synced"].append({"scenario": str(name), "ok": True})
            except Exception as exc:
                out["yjs_synced"].append({"scenario": str(name), "ok": False, "error": str(exc)})
        # Same as install(): do not rely on event bus subscriptions in CLI.
        try:
            asyncio.run(
                rebuild_webspace_from_sources(
                    target_webspace,
                    action="cli_setup_update_sync",
                    source_of_truth="scenario_projection",
                )
            )
        except Exception as exc:
            out["warnings"].append(f"webspace rebuild: {exc}")

    if json_output:
        typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
        raise typer.Exit(0 if not out["warnings"] else 1)

    typer.secho("[AdaOS] update complete", fg=typer.colors.GREEN)
    if out["warnings"]:
        typer.secho("warnings:", fg=typer.colors.YELLOW)
        for w in out["warnings"]:
            typer.echo(f"  - {w}")


autostart_app = typer.Typer(help="OS autostart integration (Windows Task / systemd --user / launchd)")


def _repo_git_text(*args: str) -> str:
    try:
        repo_root = get_ctx().paths.repo_root()
    except Exception:
        repo_root = None
    if callable(repo_root):
        repo_root = repo_root()
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return (completed.stdout or "").strip()
    except Exception:
        return ""


def _autostart_admin_base_url(token: Optional[str] = None) -> str:
    explicit = str(os.getenv("ADAOS_CONTROL_URL") or os.getenv("ADAOS_CONTROL_BASE") or "").strip()
    if explicit:
        return explicit.rstrip("/")

    ctx = get_ctx()
    status = autostart_status(ctx)
    candidates: list[str] = []
    seen: set[str] = set()

    def _push(raw: Optional[str]) -> None:
        value = str(raw or "").strip().rstrip("/")
        if not value or value in seen:
            return
        seen.add(value)
        candidates.append(value)

    _push(status.get("live_url"))
    _push(status.get("url"))

    conf = getattr(ctx, "config", None)
    bind = _resolve_stop_bind(conf)
    if bind is not None:
        host, port = bind
        _push(f"http://{host}:{int(port)}")
    _push(resolve_control_base_url())

    resolved_token = resolve_control_token(explicit=token)
    for base in candidates:
        code, _payload = probe_control_api(base_url=base, token=resolved_token, timeout_s=0.75)
        if code is not None:
            return base
    return candidates[0] if candidates else resolve_control_base_url()


def _autostart_admin_headers(token: Optional[str] = None) -> dict[str, str]:
    ctx = get_ctx()
    conf = getattr(ctx, "config", None)
    resolved = str(token or getattr(conf, "token", None) or os.getenv("ADAOS_TOKEN") or "").strip()
    headers = {"Content-Type": "application/json"}
    if resolved:
        headers["X-AdaOS-Token"] = resolved
    return headers


def _autostart_supervisor_base_url() -> str | None:
    explicit = str(os.getenv("ADAOS_SUPERVISOR_URL") or os.getenv("ADAOS_SUPERVISOR_BASE") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    try:
        info = autostart_status(get_ctx())
    except Exception:
        info = None
    if not isinstance(info, dict):
        return None
    wrapper_env = info.get("wrapper_env") if isinstance(info.get("wrapper_env"), dict) else {}
    host = str(wrapper_env.get("ADAOS_SUPERVISOR_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    raw_port = str(wrapper_env.get("ADAOS_SUPERVISOR_PORT") or "").strip()
    if not raw_port:
        return None
    try:
        port = int(raw_port)
    except Exception:
        return None
    if port <= 0:
        return None
    return f"http://{host}:{port}"


def _autostart_service_diagnostics() -> tuple[str, str]:
    journal_cmd = "journalctl --user -u adaos.service -n 120 --no-pager"
    status_path = str(core_update_status_path())
    try:
        info = autostart_status(get_ctx())
    except Exception:
        return journal_cmd, status_path

    if isinstance(info, dict):
        scope = str(info.get("scope") or "").strip().lower()
        if scope == "system":
            journal_cmd = "journalctl -u adaos.service -n 120 --no-pager"
        base_dir_raw = str(info.get("base_dir") or "").strip()
        if base_dir_raw:
            try:
                status_path = str((Path(base_dir_raw).expanduser().resolve() / "state" / "core_update" / "status.json"))
            except Exception:
                pass
    return journal_cmd, status_path


def _autostart_admin_unavailable_message(base_url: str) -> str:
    journal_cmd, status_path = _autostart_service_diagnostics()
    return (
        f"local AdaOS admin API is unavailable at {base_url}; the service may be restarting or failed to boot. "
        f"Inspect '{journal_cmd}' and '{status_path}'."
    )


def _autostart_supervisor_get(path: str, *, token: Optional[str] = None) -> dict:
    base_url = _autostart_supervisor_base_url()
    if not base_url:
        raise RuntimeError("local AdaOS supervisor API is unavailable; no supervisor base URL is configured")
    try:
        response = requests.get(base_url + path, headers=_autostart_admin_headers(token), timeout=15)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"ok": True, "response": payload}
    except RequestException as exc:
        raise RuntimeError(_autostart_admin_unavailable_message(base_url)) from exc


def _autostart_supervisor_post(path: str, *, body: dict | None = None, token: Optional[str] = None) -> dict:
    base_url = _autostart_supervisor_base_url()
    if not base_url:
        raise RuntimeError("local AdaOS supervisor API is unavailable; no supervisor base URL is configured")
    try:
        response = requests.post(
            base_url + path,
            headers=_autostart_admin_headers(token),
            json=body or {},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"ok": True, "response": payload}
    except RequestException as exc:
        raise RuntimeError(_autostart_admin_unavailable_message(base_url)) from exc


def _autostart_admin_get(path: str, *, token: Optional[str] = None) -> dict:
    try:
        base_url = _autostart_admin_base_url(token=token)
        response = requests.get(base_url + path, headers=_autostart_admin_headers(token), timeout=15)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"ok": True, "response": payload}
    except RequestException as exc:
        raise RuntimeError(_autostart_admin_unavailable_message(locals().get("base_url", "unknown"))) from exc


def _autostart_admin_post(path: str, *, body: dict | None = None, token: Optional[str] = None) -> dict:
    try:
        base_url = _autostart_admin_base_url(token=token)
        response = requests.post(
            base_url + path,
            headers=_autostart_admin_headers(token),
            json=body or {},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"ok": True, "response": payload}
    except RequestException as exc:
        raise RuntimeError(_autostart_admin_unavailable_message(locals().get("base_url", "unknown"))) from exc


def _read_json_file(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _local_autostart_update_payload() -> dict | None:
    candidate_roots: list[Path] = []
    seen: set[Path] = set()

    def _push(root: Path | None) -> None:
        if root is None:
            return
        try:
            resolved = root.expanduser().resolve()
        except Exception:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        candidate_roots.append(resolved)

    _push(core_update_status_path().parent)
    try:
        info = autostart_status(get_ctx())
    except Exception:
        info = None
    if isinstance(info, dict):
        base_dir_raw = str(info.get("base_dir") or "").strip()
        if base_dir_raw:
            _push(Path(base_dir_raw) / "state" / "core_update")

    selected_root: Path | None = None
    for root in candidate_roots:
        if any((root / name).exists() for name in ("status.json", "plan.json", "last_result.json")):
            selected_root = root
            break
    if selected_root is None:
        return None
    status_path = selected_root / "status.json"
    plan_path = selected_root / "plan.json"
    last_result_path = selected_root / "last_result.json"
    return {
        "ok": True,
        "status": _read_json_file(status_path) or {"state": "idle", "updated_at": time.time()},
        "last_result": _read_json_file(last_result_path),
        "plan": _read_json_file(plan_path),
        "slots": core_slot_status(),
        "active_manifest": active_slot_manifest(),
        "_local_fallback": True,
        "_local_fallback_root": str(selected_root),
    }


def _autostart_update_get(*, token: Optional[str] = None) -> dict:
    try:
        return _autostart_supervisor_get("/api/supervisor/update/status", token=token)
    except RuntimeError:
        return _autostart_admin_get("/api/admin/update/status", token=token)


def _autostart_update_post(path: str, *, body: dict | None = None, token: Optional[str] = None) -> dict:
    try:
        return _autostart_supervisor_post(path, body=body, token=token)
    except RuntimeError:
        runtime_path = path.replace("/api/supervisor/update/", "/api/admin/update/")
        return _autostart_admin_post(runtime_path, body=body, token=token)


def _autostart_bind_from_status(status: dict) -> tuple[str, int] | None:
    candidates = [status.get("live_url"), status.get("url"), status.get("configured_url")]
    for candidate in candidates:
        raw = str(candidate or "").strip()
        if not raw:
            continue
        try:
            parsed = urlparse(raw)
        except Exception:
            continue
        host = str(parsed.hostname or "").strip()
        port = int(parsed.port or 0)
        if host and port > 0:
            return host, port
    return None


def _safe_proc_name(proc: psutil.Process) -> str:
    try:
        return str(proc.name() or "").strip()
    except Exception:
        return ""


def _safe_proc_cmdline(proc: psutil.Process) -> list[str]:
    try:
        return [str(part) for part in proc.cmdline()]
    except Exception:
        return []


def _safe_proc_create_time(proc: psutil.Process) -> float | None:
    try:
        value = float(proc.create_time())
    except Exception:
        return None
    return value if value > 0 else None


def _format_cmdline(cmdline: list[str], *, max_len: int = 220) -> str:
    raw = " ".join(str(part) for part in (cmdline or []) if str(part).strip())
    if len(raw) <= max_len:
        return raw
    return raw[: max(0, max_len - 3)].rstrip() + "..."


def _classify_process(proc: psutil.Process) -> str:
    cmdline = [part.lower() for part in _safe_proc_cmdline(proc)]
    joined = " ".join(cmdline)
    name = _safe_proc_name(proc).lower()
    if "adaos.apps.autostart_runner" in joined:
        return "autostart_runner"
    if "adaos" in joined and "api" in joined and "serve" in joined:
        return "api_server"
    if "runtime_runner" in joined or "skill" in joined:
        return "skill_runtime"
    if "realtime" in joined or "sidecar" in joined:
        return "realtime_sidecar"
    if "node" in name:
        return "node"
    if "python" in name or "python" in joined:
        return "python"
    return name or "process"


def _proc_snapshot(proc: psutil.Process, *, now: float) -> dict:
    pid = int(getattr(proc, "pid", 0) or 0)
    with proc.oneshot():
        cmdline = _safe_proc_cmdline(proc)
        create_time = _safe_proc_create_time(proc)
        cpu_percent = None
        try:
            cpu_percent = float(proc.cpu_percent(None))
        except Exception:
            cpu_percent = None
        rss_bytes = None
        try:
            rss_bytes = int(proc.memory_info().rss)
        except Exception:
            rss_bytes = None
        status = ""
        try:
            status = str(proc.status() or "").strip()
        except Exception:
            status = ""
        threads = None
        try:
            threads = int(proc.num_threads())
        except Exception:
            threads = None
    age_sec = None
    if create_time is not None:
        age_sec = max(0.0, now - create_time)
    return {
        "pid": pid,
        "kind": _classify_process(proc),
        "name": _safe_proc_name(proc),
        "status": status or None,
        "cpu_percent": cpu_percent,
        "rss_bytes": rss_bytes,
        "threads": threads,
        "age_sec": age_sec,
        "cmdline": cmdline,
        "cmdline_text": _format_cmdline(cmdline),
    }


def _prime_cpu_metrics(proc: psutil.Process, *, include_children: bool = True) -> None:
    try:
        proc.cpu_percent(None)
    except Exception:
        pass
    if not include_children:
        return
    try:
        for child in proc.children(recursive=True):
            try:
                child.cpu_percent(None)
            except Exception:
                continue
    except Exception:
        pass


def _select_autostart_target_pid(status: dict, host: str, port: int) -> int | None:
    protected_pids = _current_process_family_pids()
    pidfile = _pidfile_path(host, port)
    meta = _read_pidfile(pidfile)
    candidates: list[int] = []
    try:
        file_pid = int((meta or {}).get("pid") or 0)
    except Exception:
        file_pid = 0
    if file_pid > 0 and file_pid not in protected_pids:
        candidates.append(file_pid)
    owner_pid = _find_listening_server_pid(host, port)
    if owner_pid and owner_pid not in protected_pids and owner_pid not in candidates:
        candidates.append(owner_pid)
    for pid in _find_matching_server_pids(host, port, protected_pids=protected_pids):
        if pid not in candidates:
            candidates.append(pid)
    active_pid = status.get("pid")
    try:
        active_pid_int = int(active_pid or 0)
    except Exception:
        active_pid_int = 0
    if active_pid_int > 0 and active_pid_int not in protected_pids and active_pid_int not in candidates:
        candidates.append(active_pid_int)
    for pid in candidates:
        try:
            proc = psutil.Process(pid)
            proc.status()
            return pid
        except Exception:
            continue
    return None


def _maybe_fetch_service_snapshot(token: Optional[str] = None) -> dict | None:
    try:
        payload = _autostart_admin_get("/api/services", token=token)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _collect_autostart_inspect(*, sample_sec: float = 0.2, token: Optional[str] = None) -> dict:
    ctx = get_ctx()
    status = autostart_status(ctx)
    bind = _autostart_bind_from_status(status)
    if bind is None:
        conf = getattr(ctx, "config", None)
        bind = _resolve_stop_bind(conf)
    target_pid = None
    if bind is not None:
        target_pid = _select_autostart_target_pid(status, bind[0], bind[1])

    process_payload: dict | None = None
    if target_pid is not None:
        proc = psutil.Process(target_pid)
        _prime_cpu_metrics(proc, include_children=True)
        sleep_s = max(0.0, min(float(sample_sec), 2.0))
        if sleep_s > 0:
            time.sleep(sleep_s)
        now = time.time()
        root = _proc_snapshot(proc, now=now)
        children: list[dict] = []
        try:
            for child in proc.children(recursive=True):
                children.append(_proc_snapshot(child, now=now))
        except Exception:
            children = []
        children.sort(
            key=lambda item: (
                float(item.get("cpu_percent") or 0.0),
                float(item.get("rss_bytes") or 0),
                int(item.get("pid") or 0),
            ),
            reverse=True,
        )
        process_payload = {
            "pid": target_pid,
            "root": root,
            "children": children,
            "top_children": children[:8],
            "child_count": len(children),
            "sample_sec": sleep_s,
        }

    services_payload = _maybe_fetch_service_snapshot(token=token)
    top_services: list[dict] = []
    if isinstance(services_payload, dict):
        raw_services = services_payload.get("services")
        if isinstance(raw_services, list):
            for item in raw_services:
                if not isinstance(item, dict):
                    continue
                service = {
                    "name": str(item.get("name") or "").strip() or "<unknown>",
                    "running": bool(item.get("running")),
                    "pid": item.get("pid"),
                    "base_url": item.get("base_url"),
                    "health_ok": item.get("health_ok"),
                }
                top_services.append(service)
        top_services.sort(key=lambda item: (0 if item.get("running") else 1, str(item.get("name") or "")))

    return {
        "autostart": status,
        "bind": {"host": bind[0], "port": bind[1]} if bind is not None else None,
        "process": process_payload,
        "services": top_services,
    }


def _format_bytes(value: object) -> str:
    try:
        size = float(value)
    except Exception:
        return "-"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)}{units[idx]}"
    return f"{size:.1f}{units[idx]}"


def _format_age(value: object) -> str:
    try:
        seconds = int(float(value))
    except Exception:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def _print_autostart_inspect(payload: dict) -> None:
    status = payload.get("autostart") if isinstance(payload.get("autostart"), dict) else {}
    bind = payload.get("bind") if isinstance(payload.get("bind"), dict) else {}
    process = payload.get("process") if isinstance(payload.get("process"), dict) else {}
    root = process.get("root") if isinstance(process.get("root"), dict) else {}
    top_children = process.get("top_children") if isinstance(process.get("top_children"), list) else []
    services = payload.get("services") if isinstance(payload.get("services"), list) else []

    enabled = status.get("enabled")
    active = status.get("active")
    listening = status.get("listening")
    typer.echo(
        f"autostart: enabled={enabled} active={active} listening={listening}"
    )
    if status.get("url"):
        typer.echo(f"url: {status.get('url')}")
    if bind:
        typer.echo(f"bind: {bind.get('host')}:{bind.get('port')}")
    if root:
        typer.echo(
            "process: "
            f"pid={root.get('pid')} kind={root.get('kind')} status={root.get('status') or '-'} "
            f"cpu={float(root.get('cpu_percent') or 0.0):.1f}% rss={_format_bytes(root.get('rss_bytes'))} "
            f"threads={root.get('threads') or '-'} age={_format_age(root.get('age_sec'))}"
        )
        if root.get("cmdline_text"):
            typer.echo(f"cmd: {root.get('cmdline_text')}")
    else:
        typer.echo("process: not found")

    if top_children:
        typer.echo("top children:")
        for child in top_children:
            if not isinstance(child, dict):
                continue
            typer.echo(
                "  "
                f"pid={child.get('pid')} kind={child.get('kind')} cpu={float(child.get('cpu_percent') or 0.0):.1f}% "
                f"rss={_format_bytes(child.get('rss_bytes'))} threads={child.get('threads') or '-'} "
                f"age={_format_age(child.get('age_sec'))} cmd={child.get('cmdline_text') or '-'}"
            )

    if services:
        typer.echo("services:")
        for service in services[:12]:
            if not isinstance(service, dict):
                continue
            status_text = "running" if service.get("running") else "stopped"
            health = service.get("health_ok")
            health_text = ""
            if health is True:
                health_text = " health=ok"
            elif health is False:
                health_text = " health=fail"
            typer.echo(
                "  "
                f"{service.get('name')}: {status_text} pid={service.get('pid') or '-'} "
                f"{service.get('base_url') or '-'}{health_text}"
            )


@autostart_app.command("status")
@_run_safe
def autostart_status_cmd(json_output: bool = typer.Option(False, "--json", help=_("cli.option.json"))):
    ctx = get_ctx()
    s = autostart_status(ctx)
    if json_output:
        typer.echo(json.dumps(s, ensure_ascii=False, indent=2))
    else:
        enabled = s.get("enabled")
        active = s.get("active")
        listening = s.get("listening")
        msg = f"enabled: {enabled}" + (f", active: {active}" if active is not None else "")
        if listening is not None:
            msg += f", listening: {listening}"
        typer.echo(msg)
        if "url" in s:
            typer.echo(f"url: {s['url']}")
        if "supervisor_url" in s:
            typer.echo(f"supervisor url: {s['supervisor_url']}")
        if "configured_url" in s:
            typer.echo(f"configured url: {s['configured_url']}")
        if "live_url" in s:
            typer.echo(f"live url: {s['live_url']}")
        if "service" in s:
            typer.echo(f"service: {s['service']}")
        if "scope" in s:
            typer.echo(f"scope: {s['scope']}")
        if "base_dir" in s:
            typer.echo(f"base dir: {s['base_dir']}")
        if "shared_dotenv_path" in s:
            typer.echo(f"shared dotenv: {s['shared_dotenv_path']}")
        if "system_scope_preferred" in s:
            typer.echo(f"system scope preferred: {s['system_scope_preferred']}")
        if "user_service_exists" in s or "system_service_exists" in s:
            typer.echo(
                f"user service exists: {s.get('user_service_exists')}, system service exists: {s.get('system_service_exists')}"
            )
        if "task" in s:
            typer.echo(f"task: {s['task']}")
        if "plist" in s:
            typer.echo(f"plist: {s['plist']}")
        if "wrapper" in s:
            typer.echo(f"wrapper: {s['wrapper']}")
        core_update = s.get("core_update_status")
        if isinstance(core_update, dict):
            state = core_update.get("state")
            phase = core_update.get("phase")
            message = core_update.get("message")
            summary = f"last runner status: state={state}"
            if phase:
                summary += f", phase={phase}"
            if message:
                summary += f", message={message}"
            typer.echo(summary)


@autostart_app.command("inspect")
@_run_safe
def autostart_inspect_cmd(
    sample_sec: float = typer.Option(
        0.2,
        "--sample-sec",
        min=0.0,
        max=2.0,
        help="CPU sampling window for process diagnostics.",
    ),
    token: Optional[str] = typer.Option(None, "--token", help="Override X-AdaOS-Token for local admin API"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    payload = _collect_autostart_inspect(sample_sec=sample_sec, token=token)
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    _print_autostart_inspect(payload)


@autostart_app.command("enable")
@_run_safe
def autostart_enable_cmd(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8777, "--port"),
    token: Optional[str] = typer.Option(None, "--token", help="X-AdaOS-Token (stored in service environment)"),
    scope: str = typer.Option("auto", "--scope", help="Linux: auto|user|system"),
    run_as: Optional[str] = typer.Option(None, "--run-as", help="Linux (system scope): run service as this user"),
    create_user: bool = typer.Option(False, "--create-user", help="Linux: create --run-as user if missing (requires root)"),
    base_dir: Optional[str] = typer.Option(None, "--base-dir", help="Override ADAOS_BASE_DIR for autostart"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    ctx = get_ctx()
    spec = default_autostart_spec(ctx, host=host, port=port, token=token)
    if base_dir:
        spec.env["ADAOS_BASE_DIR"] = str(base_dir)
    try:
        res = autostart_enable(
            ctx,
            spec,
            scope=str(scope or "auto").strip().lower(),
            run_as=run_as,
            create_user=bool(create_user),
        )
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        typer.secho("[AdaOS] autostart enabled", fg=typer.colors.GREEN)
        for k in ("scope", "run_as", "task", "service", "plist", "wrapper"):
            if k in res:
                typer.echo(f"{k}: {res[k]}")


@autostart_app.command("disable")
@_run_safe
def autostart_disable_cmd(json_output: bool = typer.Option(False, "--json", help=_("cli.option.json"))):
    ctx = get_ctx()
    res = autostart_disable(ctx)
    if json_output:
        typer.echo(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        typer.secho("[AdaOS] autostart disabled", fg=typer.colors.GREEN)


@autostart_app.command("update-status")
@_run_safe
def autostart_update_status_cmd(
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
    token: Optional[str] = typer.Option(None, "--token", help="Override X-AdaOS-Token for local admin API"),
):
    try:
        payload = _autostart_update_get(token=token)
    except RuntimeError as exc:
        payload = _local_autostart_update_payload()
        if payload is None:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    slots = payload.get("slots") if isinstance(payload.get("slots"), dict) else {}
    active_manifest_payload = payload.get("active_manifest") if isinstance(payload.get("active_manifest"), dict) else {}
    active_slot = str(slots.get("active_slot") or "")
    previous_slot = str(slots.get("previous_slot") or "")
    slot_map = slots.get("slots") if isinstance(slots.get("slots"), dict) else {}

    def _slot_manifest(slot_id: str) -> dict:
        if not slot_id:
            return {}
        slot_meta = slot_map.get(slot_id)
        if not isinstance(slot_meta, dict):
            return dict(active_manifest_payload) if slot_id == active_slot and active_manifest_payload else {}
        manifest = slot_meta.get("manifest")
        if isinstance(manifest, dict) and manifest:
            return manifest
        if slot_id == active_slot and active_manifest_payload:
            return dict(active_manifest_payload)
        return {}

    def _format_slot_line(slot_id: str) -> str:
        manifest = _slot_manifest(slot_id)
        if not slot_id:
            return "--"
        version = str(manifest.get("target_version") or "").strip()
        short_commit = str(manifest.get("git_short_commit") or "").strip()
        if not short_commit:
            full_commit = str(manifest.get("git_commit") or "").strip()
            short_commit = full_commit[:8] if full_commit else ""
        branch = str(manifest.get("git_branch") or manifest.get("target_rev") or "").strip()
        parts = [slot_id]
        if version:
            parts.append(version)
        if short_commit:
            parts.append(short_commit)
        if branch:
            parts.append(branch)
        return " | ".join(parts)

    typer.echo(f"state: {status.get('state') or 'unknown'}")
    if status.get("phase"):
        typer.echo(f"phase: {status.get('phase')}")
    if status.get("message"):
        typer.echo(f"message: {status.get('message')}")
    if status.get("target_rev"):
        typer.echo(f"target rev: {status.get('target_rev')}")
    if status.get("target_version"):
        typer.echo(f"target version: {status.get('target_version')}")
    typer.echo(f"active slot: {_format_slot_line(active_slot)}")
    typer.echo(f"previous slot: {_format_slot_line(previous_slot)}")
    active_manifest = _slot_manifest(active_slot)
    if active_manifest.get("git_commit"):
        typer.echo(f"active commit: {active_manifest.get('git_commit')}")
    if active_manifest.get("git_subject"):
        typer.echo(f"active subject: {active_manifest.get('git_subject')}")


@autostart_app.command("update-start")
@_run_safe
def autostart_update_start_cmd(
    target_rev: str = typer.Option("", "--target-rev", help="Git branch/tag/ref to install; defaults to current branch"),
    target_version: str = typer.Option("", "--target-version", help="Human-readable target version; defaults to current BUILD_INFO.version"),
    countdown_sec: float = typer.Option(60.0, "--countdown-sec", min=0.0),
    drain_timeout_sec: float = typer.Option(10.0, "--drain-timeout-sec", min=0.0, max=30.0),
    signal_delay_sec: float = typer.Option(0.25, "--signal-delay-sec", min=0.0, max=5.0),
    reason: str = typer.Option("cli.core_update", "--reason"),
    token: Optional[str] = typer.Option(None, "--token", help="Override X-AdaOS-Token for local admin API"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    resolved_rev = str(target_rev or _repo_git_text("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    resolved_version = str(target_version or BUILD_INFO.version or "").strip()
    try:
        payload = _autostart_update_post(
            "/api/supervisor/update/start",
            token=token,
            body={
                "target_rev": resolved_rev,
                "target_version": resolved_version,
                "countdown_sec": countdown_sec,
                "drain_timeout_sec": drain_timeout_sec,
                "signal_delay_sec": signal_delay_sec,
                "reason": reason,
            },
        )
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    typer.echo(json.dumps(payload, ensure_ascii=False))


@autostart_app.command("update-cancel")
@_run_safe
def autostart_update_cancel_cmd(
    reason: str = typer.Option("cli.core_update.cancel", "--reason"),
    token: Optional[str] = typer.Option(None, "--token", help="Override X-AdaOS-Token for local admin API"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    try:
        payload = _autostart_update_post("/api/supervisor/update/cancel", token=token, body={"reason": reason})
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    typer.echo(json.dumps(payload, ensure_ascii=False))


@autostart_app.command("update-rollback")
@_run_safe
def autostart_update_rollback_cmd(
    countdown_sec: float = typer.Option(0.0, "--countdown-sec", min=0.0),
    drain_timeout_sec: float = typer.Option(10.0, "--drain-timeout-sec", min=0.0, max=30.0),
    signal_delay_sec: float = typer.Option(0.25, "--signal-delay-sec", min=0.0, max=5.0),
    reason: str = typer.Option("cli.core_update.rollback", "--reason"),
    token: Optional[str] = typer.Option(None, "--token", help="Override X-AdaOS-Token for local admin API"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    try:
        payload = _autostart_update_post(
            "/api/supervisor/update/rollback",
            token=token,
            body={
                "countdown_sec": countdown_sec,
                "drain_timeout_sec": drain_timeout_sec,
                "signal_delay_sec": signal_delay_sec,
                "reason": reason,
            },
        )
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    typer.echo(json.dumps(payload, ensure_ascii=False))


@autostart_app.command("smoke-update")
@_run_safe
def autostart_smoke_update_cmd(
    target_rev: str = typer.Option("", "--target-rev", help="Defaults to current git branch when available"),
    target_version: str = typer.Option("", "--target-version", help="Defaults to current BUILD_INFO.version"),
    countdown_sec: float = typer.Option(5.0, "--countdown-sec", min=0.0),
    drain_timeout_sec: float = typer.Option(10.0, "--drain-timeout-sec", min=0.0, max=30.0),
    signal_delay_sec: float = typer.Option(0.25, "--signal-delay-sec", min=0.0, max=5.0),
    token: Optional[str] = typer.Option(None, "--token", help="Override X-AdaOS-Token for local admin API"),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
):
    resolved_rev = str(target_rev or _repo_git_text("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    resolved_version = str(target_version or BUILD_INFO.version or "").strip()
    try:
        payload = _autostart_update_post(
            "/api/supervisor/update/start",
            token=token,
            body={
                "target_rev": resolved_rev,
                "target_version": resolved_version,
                "countdown_sec": countdown_sec,
                "drain_timeout_sec": drain_timeout_sec,
                "signal_delay_sec": signal_delay_sec,
                "reason": "cli.smoke_update",
            },
        )
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    typer.echo(json.dumps(payload, ensure_ascii=False))
