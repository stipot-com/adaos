from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
import sys
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence
from urllib.parse import urlparse

import requests

from adaos.build_info import BUILD_INFO
from adaos.services.agent_context import AgentContext
from adaos.services.core_slots import active_slot, activate_slot, read_slot_manifest, slot_dir
from adaos.services.runtime_paths import current_state_dir
from adaos.services.node_config import load_config
from adaos.services.settings import Settings, _parse_env_file


@dataclass(frozen=True, slots=True)
class AutostartSpec:
    name: str
    argv: tuple[str, ...]
    env: dict[str, str]


def _home() -> Path:
    return Path.home().expanduser().resolve()


def _is_windows() -> bool:
    return os.name == "nt"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _linux_euid() -> int | None:
    try:
        return int(os.geteuid())  # type: ignore[attr-defined]
    except Exception:
        return None


def _linux_is_root() -> bool:
    return _linux_euid() == 0


def _base_dir_from_spec(ctx: AgentContext, spec: AutostartSpec | None = None) -> Path:
    raw = str((spec.env.get("ADAOS_BASE_DIR") if spec else "") or "").strip()
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except Exception:
            pass
    return ctx.paths.base_dir()


def _service_settings(ctx: AgentContext) -> Settings:
    settings = getattr(ctx, "settings", None)
    profile = str(getattr(settings, "profile", "default") or "default")
    base_dir = ctx.paths.base_dir()
    base_dir = base_dir() if callable(base_dir) else base_dir
    base_dir = Path(base_dir).expanduser().resolve()

    shared_dotenv = _shared_dotenv_path(ctx)
    if shared_dotenv is None:
        if isinstance(settings, Settings):
            return settings.with_overrides(base_dir=base_dir, profile=profile)
        return Settings.from_sources().with_overrides(base_dir=base_dir, profile=profile)

    try:
        env_file_vars = _parse_env_file(str(shared_dotenv))
        profile = str(env_file_vars.get("ADAOS_PROFILE", "") or "").strip() or profile
        override_base = str(env_file_vars.get("ADAOS_BASE_DIR", "") or "").strip()
        legacy_base = str(env_file_vars.get("BASE_DIR", "") or "").strip()
        if override_base:
            base_dir = Path(override_base).expanduser().resolve()
        elif legacy_base:
            base_dir = Path(legacy_base).expanduser().resolve()
        if isinstance(settings, Settings):
            return settings.with_overrides(base_dir=base_dir, profile=profile)
        return Settings.from_sources(env_file=str(shared_dotenv)).with_overrides(base_dir=base_dir, profile=profile)
    except Exception:
        if isinstance(settings, Settings):
            return settings.with_overrides(base_dir=base_dir, profile=profile)
        return Settings.from_sources().with_overrides(base_dir=base_dir, profile=profile)


def default_spec(
    ctx: AgentContext,
    *,
    host: str = "127.0.0.1",
    port: int = 8777,
    token: str | None = None,
) -> AutostartSpec:
    service_settings = _service_settings(ctx)
    base_dir = service_settings.base_dir
    profile = getattr(service_settings, "profile", getattr(ctx.settings, "profile", "default"))
    argv = (
        sys.executable,
        "-m",
        "adaos.apps.supervisor",
        "--host",
        host,
        "--port",
        str(int(port)),
    )
    env = {
        "ADAOS_BASE_DIR": str(base_dir),
        "ADAOS_PROFILE": str(profile),
    }
    shared_dotenv = _shared_dotenv_path(ctx)
    if shared_dotenv:
        env["ADAOS_SHARED_DOTENV_PATH"] = str(shared_dotenv)
    env.setdefault("ADAOS_SUPERVISOR_HOST", "127.0.0.1")
    env.setdefault("ADAOS_SUPERVISOR_PORT", "8776")
    resolved_token = str(token or _default_control_token() or "").strip()
    if resolved_token:
        env["ADAOS_TOKEN"] = resolved_token
    return AutostartSpec(name="adaos", argv=argv, env=env)


def _default_control_token() -> str | None:
    raw = str(os.getenv("ADAOS_TOKEN") or os.getenv("ADAOS_HUB_TOKEN") or os.getenv("HUB_TOKEN") or "").strip()
    if raw:
        return raw
    try:
        conf = load_config()
    except Exception:
        conf = None
    token = str(getattr(conf, "token", "") or "").strip() if conf is not None else ""
    return token or None


def _repo_root(ctx: AgentContext) -> Path | None:
    try:
        repo_root = ctx.paths.repo_root()
        return repo_root() if callable(repo_root) else repo_root
    except Exception:
        try:
            package = ctx.paths.package_path()
            package = package() if callable(package) else package
            return Path(package).resolve().parents[1]
        except Exception:
            return None


def _shared_dotenv_path(ctx: AgentContext) -> Path | None:
    raw = str(os.getenv("ADAOS_SHARED_DOTENV_PATH") or "").strip()
    if raw:
        path = Path(raw).expanduser().resolve()
        return path if path.exists() else None
    repo_root = _repo_root(ctx)
    if repo_root is None:
        return None
    candidate = (repo_root / ".env").resolve()
    return candidate if candidate.exists() else None


def _slot_manifest_ready(slot: str | None) -> bool:
    if not slot:
        return False
    manifest = read_slot_manifest(slot)
    if not isinstance(manifest, dict):
        return False
    return bool(isinstance(manifest.get("argv"), list) or str(manifest.get("command") or "").strip())


def _bootstrap_core_slot(ctx: AgentContext, *, token: str | None = None) -> None:
    current = active_slot()
    if _slot_manifest_ready(current):
        return
    repo_root = _repo_root(ctx)
    if repo_root is None or not repo_root.exists():
        raise RuntimeError("cannot initialize core slot: repo root is not available")
    slot = current or "A"
    cmd = [
        sys.executable,
        "-m",
        "adaos.apps.core_update_apply",
        "--slot",
        slot,
        "--slot-dir",
        str(slot_dir(slot)),
        "--base-dir",
        str(ctx.paths.base_dir()),
        "--repo-root",
        str(repo_root),
        "--source-repo-root",
        str(repo_root),
        "--target-rev",
        str(os.getenv("ADAOS_REV") or os.getenv("ADAOS_INIT_REV") or "").strip(),
        "--target-version",
        str(BUILD_INFO.version or ""),
    ]
    shared_dotenv = _shared_dotenv_path(ctx)
    if shared_dotenv is not None:
        cmd.extend(["--shared-dotenv-path", str(shared_dotenv)])
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "failed to initialize bootstrap core slot\n"
            f"stdout:\n{(completed.stdout or '')[-4000:]}\n"
            f"stderr:\n{(completed.stderr or '')[-4000:]}"
        )
    activate_slot(slot)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_wrapper_windows(path: Path, *, argv: Sequence[str], env: Mapping[str, str]) -> None:
    # Keep script simple: set env vars and exec python in foreground.
    def _ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    lines = []
    for k, v in env.items():
        lines.append(f"$env:{k} = {_ps_quote(str(v))}")
    py = str(argv[0])
    lines.append(f"$py = {_ps_quote(py)}")
    lines.append("$args = @(")
    for arg in argv[1:]:
        lines.append(f"  {_ps_quote(str(arg))}")
    lines.append(")")
    lines.append("& $py @args")
    _write_text(path, "\r\n".join(lines) + "\r\n")


def _write_wrapper_sh(path: Path, *, argv: Sequence[str], env: Mapping[str, str]) -> None:
    def _sh_quote(value: str) -> str:
        return "'" + value.replace("'", "'\"'\"'") + "'"

    lines = ["#!/usr/bin/env bash", "set -euo pipefail"]
    for k, v in env.items():
        lines.append(f"export {k}={_sh_quote(str(v))}")
    quoted = " ".join(_sh_quote(str(x)) for x in argv)
    lines.append(f"exec {quoted}")
    _write_text(path, "\n".join(lines) + "\n")
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except Exception:
        pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def _is_local_url(url: str | None) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    try:
        host = str(urlparse(raw).hostname or "").strip().lower()
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _state_dir() -> Path:
    return current_state_dir()


def _tcp_probe(host: str, port: int, *, timeout: float = 0.6) -> bool:
    host = str(host or "").strip() or "127.0.0.1"
    try:
        port_i = int(port)
    except Exception:
        return False
    try:
        with socket.create_connection((host, port_i), timeout=timeout):
            return True
    except Exception:
        return False


def _local_url_to_host_port(url: str | None) -> tuple[str, int] | None:
    raw = str(url or "").strip()
    if not raw or not _is_local_url(raw):
        return None
    try:
        parsed = urlparse(raw)
        host = str(parsed.hostname or "").strip() or "127.0.0.1"
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    except Exception:
        return None
    return host, port


def _pidfile_control_candidates() -> list[tuple[float, str, int]]:
    found: list[tuple[float, str, int]] = []
    try:
        api_dir = _state_dir() / "api"
        if not api_dir.exists():
            return found
        for path in api_dir.glob("serve-*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            advertised = _local_url_to_host_port(data.get("advertised_base"))
            if advertised is None:
                continue
            try:
                started_at = float(data.get("started_at") or 0.0)
            except Exception:
                started_at = 0.0
            found.append((started_at, advertised[0], advertised[1]))
    except Exception:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    return found


def _core_update_status_from_base_dir(base_dir: Path | str | None) -> dict[str, object] | None:
    try:
        if base_dir:
            status_path = (Path(base_dir).expanduser().resolve() / "state" / "core_update" / "status.json").resolve()
            if not status_path.exists():
                return None
            payload = json.loads(status_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None
    return None


def _http_probe_local_control(host: str, port: int, *, timeout: float = 0.5) -> bool:
    base = f"http://{str(host or '127.0.0.1').strip() or '127.0.0.1'}:{int(port)}"
    sess = requests.Session()
    try:
        sess.trust_env = False
    except Exception:
        pass
    try:
        resp = sess.get(f"{base}/api/ping", headers={"Accept": "application/json"}, timeout=timeout)
        if int(resp.status_code) == 200:
            return True
    except Exception:
        pass
    token = _default_control_token()
    headers = {"Accept": "application/json"}
    if token:
        headers["X-AdaOS-Token"] = token
    try:
        resp = sess.get(f"{base}/api/node/status", headers=headers, timeout=timeout)
        return int(resp.status_code) in {200, 401, 403}
    except Exception:
        return False


def _discover_live_control_bind(configured_host: str, configured_port: int) -> tuple[str, int] | None:
    candidates: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    def _push(host: str | None, port: int | None) -> None:
        try:
            host_norm = str(host or "").strip() or "127.0.0.1"
            port_norm = int(port or 0)
        except Exception:
            return
        if port_norm <= 0:
            return
        item = (host_norm, port_norm)
        if item in seen:
            return
        seen.add(item)
        candidates.append(item)

    _push(configured_host, configured_port)
    try:
        conf = load_config()
    except Exception:
        conf = None
    if conf is not None:
        try:
            local_bind = _local_url_to_host_port(getattr(conf, "hub_url", None))
        except Exception:
            local_bind = None
        if local_bind is not None:
            _push(*local_bind)
    for _, host, port in _pidfile_control_candidates():
        _push(host, port)
    for item in (
        ("127.0.0.1", 8777),
        ("127.0.0.1", 8778),
        ("127.0.0.1", 8779),
        ("localhost", 8777),
        ("localhost", 8778),
        ("localhost", 8779),
    ):
        _push(*item)

    for host, port in candidates:
        if _http_probe_local_control(host, port):
            return host, port
    for host, port in candidates:
        if (host, port) == (configured_host, configured_port) and _tcp_probe(host, port):
            return host, port
    return None


def _linux_service_main_pid(scope: str) -> int | None:
    if not shutil_which("systemctl"):
        return None
    cmd = ["systemctl", "show", "-p", "MainPID", "--value", _linux_service_name()]
    if str(scope or "").strip().lower() == "user":
        if not _linux_systemctl_user_available():
            return None
        cmd = ["systemctl", "--user", "show", "-p", "MainPID", "--value", _linux_service_name()]
    elif not (_linux_is_root() and _linux_has_systemd_pid1()):
        return None
    try:
        proc = _run(cmd)
        if proc.returncode != 0:
            return None
        value = int(str(proc.stdout or "").strip() or "0")
    except Exception:
        return None
    return value if value > 0 else None


def _parse_wrapper_host_port(wrapper: Path) -> tuple[str, int] | None:
    """
    Best-effort extract `--host` / `--port` from our generated wrapper scripts.

    - Linux/macOS wrapper is a small bash script with an `exec ... --host X --port Y` line.
    - Windows wrapper is PowerShell and contains an args array with '--host', 'X', '--port', 'Y'.
    """
    try:
        text = wrapper.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    host = None
    port = None

    # Common patterns.
    m_host = re.search(r"(?:^|\s)--host\s+([0-9A-Za-z_.:\[\]-]+)", text, flags=re.IGNORECASE | re.MULTILINE)
    if m_host:
        host = m_host.group(1).strip().strip("'\"")
    m_port = re.search(r"(?:^|\s)--port\s+([0-9]{2,6})", text, flags=re.IGNORECASE | re.MULTILINE)
    if m_port:
        try:
            port = int(m_port.group(1))
        except Exception:
            port = None

    if not host or not port:
        return None
    return host, port


def _parse_wrapper_env(wrapper: Path) -> dict[str, str]:
    try:
        text = wrapper.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}

    env: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = re.match(r"^\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(['\"])(.*)\2\s*$", line)
        if m:
            key = m.group(1)
            value = m.group(3)
            quote = m.group(2)
            if quote == "'":
                value = value.replace("''", "'")
            env[key] = value
            continue

        m = re.match(r"^export\s+([A-Za-z_][A-Za-z0-9_]*)=(['\"])(.*)\2\s*$", line)
        if m:
            key = m.group(1)
            value = m.group(3)
            quote = m.group(2)
            if quote == "'":
                value = value.replace("'\"'\"'", "'")
            env[key] = value
            continue

    return env


def _windows_task_name() -> str:
    return "AdaOS"


def _parse_windows_task_info(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key:
            out[key] = value
    return out


def _extract_task_wrapper_from_command(command: str | None) -> str | None:
    raw = str(command or "").strip()
    if not raw:
        return None
    m = re.search(r'-File\s+"([^"]+)"', raw, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"-File\s+'([^']+)'", raw, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"-File\s+(\S+)", raw, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip().strip("'\"")
    return None


def _linux_service_name() -> str:
    return "adaos.service"


def _macos_label() -> str:
    return "com.adaos.autostart"


def _linux_service_path_user() -> Path:
    return (_home() / ".config" / "systemd" / "user" / _linux_service_name()).resolve()


def _linux_service_path_system() -> Path:
    return (Path("/etc/systemd/system") / _linux_service_name()).resolve()


def _linux_has_systemd_pid1() -> bool:
    try:
        comm = Path("/proc/1/comm").read_text(encoding="utf-8", errors="replace").strip().lower()
    except Exception:
        comm = ""
    if comm == "systemd":
        return True
    try:
        exe = Path("/proc/1/exe").resolve()
    except Exception:
        exe = None
    return bool(exe and exe.name.lower().startswith("systemd"))


def _linux_user_bus_path() -> Path | None:
    """
    systemctl --user talks to the user's systemd manager over the session D-Bus.
    In most distros that means a socket at $XDG_RUNTIME_DIR/bus.
    """
    xdg = str(os.getenv("XDG_RUNTIME_DIR") or "").strip()
    if xdg:
        try:
            candidate = (Path(xdg).expanduser().resolve() / "bus").resolve()
        except Exception:
            candidate = None
        if candidate is not None and candidate.exists():
            return candidate
    # Fallback for environments where XDG_RUNTIME_DIR isn't set but the address is.
    addr = str(os.getenv("DBUS_SESSION_BUS_ADDRESS") or "").strip()
    m = re.search(r"unix:path=([^,]+)", addr)
    if m:
        try:
            candidate = Path(m.group(1)).expanduser().resolve()
        except Exception:
            candidate = None
        if candidate is not None and candidate.exists():
            return candidate
    return None


def _linux_systemctl_user_available() -> bool:
    if not _is_linux():
        return False
    if not shutil_which("systemctl"):
        return False
    return _linux_user_bus_path() is not None


def _linux_systemctl_system_available() -> bool:
    if not _is_linux():
        return False
    if not shutil_which("systemctl"):
        return False
    return _linux_is_root() and _linux_has_systemd_pid1()


def _linux_systemctl_user_unavailable_hint(*, user_service_path: Path, wrapper: Path) -> str:
    bus = _linux_user_bus_path()
    bus_str = str(bus) if bus is not None else ""
    parts = [
        "systemctl --user is not available (no user session D-Bus).",
        "",
        "This usually happens when running as root, over SSH without a login session, or inside a container without systemd.",
        "",
        f"Generated files (already written):",
        f"- service: {user_service_path}",
        f"- wrapper: {wrapper}",
        "",
        "Fix options:",
        "- Run AdaOS under a regular user with a proper login session.",
        "- If you need it to run without logging in, enable lingering: `loginctl enable-linger <user>` and re-login.",
        "- Ensure `XDG_RUNTIME_DIR` points to `/run/user/<uid>` and the bus socket exists (typically `/run/user/<uid>/bus`).",
        "- If you're in a Proxmox CT/LXC: enable systemd in the container, or use the system service mode (root).",
    ]
    if bus_str:
        parts.append(f"- Detected bus socket: {bus_str}")
    parts += [
        "",
        "After the session bus is available, you can enable the service manually:",
        f"- `systemctl --user daemon-reload`",
        f"- `systemctl --user enable --now {_linux_service_name()}`",
    ]
    return "\n".join(parts).strip()


def _linux_systemctl_system_unavailable_hint(*, wrapper: Path) -> str:
    parts = [
        "systemctl (system scope) is not available.",
        "",
        "This usually means one of:",
        "- you're not root, or",
        "- PID 1 is not systemd (common in LXC containers without systemd), or",
        "- systemd isn't running properly inside the container.",
        "",
        f"Wrapper script (already written): {wrapper}",
        "",
        "Fix options (Proxmox CT/LXC):",
        "- Enable systemd inside the container (often requires `nesting=1` and `keyctl=1`) and reboot CT.",
        "- Or use a different supervisor (cron, pm2, etc.) instead of systemd.",
    ]
    return "\n".join(parts).strip()


def _linux_should_prefer_system_scope(scope: Literal["auto", "user", "system"], *, run_as: str | None = None) -> bool:
    scope_norm = str(scope or "auto").strip().lower()
    run_as_user = str(run_as or "").strip()
    if scope_norm == "system":
        return True
    if scope_norm != "auto":
        return False
    if run_as_user:
        return True
    return bool(_linux_is_root() and _linux_has_systemd_pid1())


def _best_effort_remove(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _linux_write_service_file(
    service_path: Path,
    *,
    wrapper: Path,
    scope: Literal["user", "system"],
    run_as: str | None = None,
) -> None:
    unit_lines = [
        "[Unit]",
        "Description=AdaOS",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={wrapper}",
        "Restart=always",
        "RestartSec=3",
        "Environment=PYTHONUNBUFFERED=1",
    ]
    if scope == "system" and run_as:
        unit_lines += [f"User={run_as}", f"Group={run_as}"]
    unit_lines += [
        "",
        "[Install]",
        "WantedBy=default.target" if scope == "user" else "WantedBy=multi-user.target",
        "",
    ]
    _write_text(
        service_path,
        "\n".join(unit_lines),
    )


def _linux_user_exists(username: str) -> bool:
    name = str(username or "").strip()
    if not name:
        return False
    proc = _run(["id", "-u", name])
    return proc.returncode == 0


def _linux_create_system_user(username: str) -> None:
    """
    Best-effort creation of a service user for Ubuntu/Debian environments.
    """
    name = str(username or "").strip()
    if not name:
        raise RuntimeError("invalid username")
    if _linux_user_exists(name):
        return
    proc = _run(["useradd", "--system", "--create-home", "--shell", "/usr/sbin/nologin", name])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"failed to create user: {name}").strip())


def _linux_paths_safe_for_run_as(spec: AutostartSpec, *, run_as: str) -> tuple[bool, str]:
    """
    Running as a non-root user can't work if the Python executable or base dir
    lives under /root (common in dev installs).
    """
    user = str(run_as or "").strip()
    if not user or user == "root":
        return True, ""
    python_path = str(spec.argv[0] or "").strip()
    base_dir = str(spec.env.get("ADAOS_BASE_DIR") or "").strip()
    shared_dotenv = str(spec.env.get("ADAOS_SHARED_DOTENV_PATH") or "").strip()
    bad: list[str] = []
    for label, value in [
        ("python", python_path),
        ("ADAOS_BASE_DIR", base_dir),
        ("ADAOS_SHARED_DOTENV_PATH", shared_dotenv),
    ]:
        if value.startswith("/root/") or value == "/root" or value.startswith("/root\\"):
            bad.append(f"{label}={value}")
    if not bad:
        return True, ""
    hint = "\n".join(
        [
            "cannot run AdaOS as a non-root user because paths point to /root:",
            *[f"- {x}" for x in bad],
            "",
            "Fix options:",
            "- Install AdaOS/venv outside /root (e.g. /opt/adaos) and use a base dir like /var/lib/adaos.",
            "- Or run autostart as root (system scope) without --run-as.",
            "",
            "Tip: you can override the base dir for autostart via `adaos autostart enable --base-dir /var/lib/adaos`.",
        ]
    ).strip()
    return False, hint


def enable(
    ctx: AgentContext,
    spec: AutostartSpec,
    *,
    force: bool = True,
    scope: Literal["auto", "user", "system"] = "auto",
    run_as: str | None = None,
    create_user: bool = False,
) -> dict:
    scope_norm = str(scope or "auto").strip().lower()
    if scope_norm not in {"auto", "user", "system"}:
        raise RuntimeError(f"invalid autostart scope: {scope!r} (expected auto|user|system)")
    scope = scope_norm  # type: ignore[assignment]

    _bootstrap_core_slot(ctx, token=spec.env.get("ADAOS_TOKEN"))
    base_dir = _base_dir_from_spec(ctx, spec)
    bin_dir = (base_dir / "bin").resolve()
    bin_dir.mkdir(parents=True, exist_ok=True)

    if _is_windows():
        wrapper = (bin_dir / "adaos-autostart.ps1").resolve()
        _write_wrapper_windows(wrapper, argv=spec.argv, env=spec.env)
        name = _windows_task_name()
        task_cmd = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{wrapper}"'
        args = ["schtasks", "/Create"]
        if force:
            args.append("/F")
        args += ["/TN", name, "/TR", task_cmd, "/SC", "ONLOGON"]
        proc = _run(args)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "failed to create scheduled task").strip())
        return {"ok": True, "platform": "windows", "wrapper": str(wrapper), "task": name}

    if _is_linux():
        run_as_user = str(run_as or "").strip() or None
        prefer_system_scope = _linux_should_prefer_system_scope(scope, run_as=run_as_user)
        if (
            prefer_system_scope
            and shutil_which("systemctl")
            and _linux_is_root()
            and _linux_has_systemd_pid1()
            and run_as_user
            and run_as_user != "root"
        ):
            ok, hint = _linux_paths_safe_for_run_as(spec, run_as=run_as_user)
            if not ok:
                raise RuntimeError(hint)

        wrapper = (bin_dir / "adaos-autostart.sh").resolve()
        _write_wrapper_sh(wrapper, argv=spec.argv, env=spec.env)
        user_service_path = _linux_service_path_user()

        if not prefer_system_scope:
            _linux_write_service_file(user_service_path, wrapper=wrapper, scope="user")

        # Prefer user service when explicitly requested, or for non-root auto mode.
        if (not prefer_system_scope) and (scope in {"auto", "user"}) and shutil_which("systemctl") and _linux_systemctl_user_available():
            _run(["systemctl", "--user", "daemon-reload"])
            enabled = _run(["systemctl", "--user", "enable", "--now", _linux_service_name()])
            if enabled.returncode != 0:
                raise RuntimeError((enabled.stderr or enabled.stdout or "failed to enable systemd user service").strip())
            return {
                "ok": True,
                "platform": "linux",
                "scope": "user",
                "wrapper": str(wrapper),
                "service": str(user_service_path),
            }

        # Prefer system scope for root / server environments where user services do not survive a reboot reliably.
        if prefer_system_scope and shutil_which("systemctl") and _linux_is_root() and _linux_has_systemd_pid1():
            if run_as_user and run_as_user != "root":
                if create_user:
                    _linux_create_system_user(run_as_user)
                elif not _linux_user_exists(run_as_user):
                    raise RuntimeError(f"user does not exist: {run_as_user} (use --create-user to create it)")

            if shutil_which("systemctl") and _linux_systemctl_user_available():
                _run(["systemctl", "--user", "disable", "--now", _linux_service_name()])
                _run(["systemctl", "--user", "daemon-reload"])
            _best_effort_remove(user_service_path)

            system_service_path = _linux_service_path_system()
            _linux_write_service_file(system_service_path, wrapper=wrapper, scope="system", run_as=run_as_user)
            _run(["systemctl", "daemon-reload"])
            enabled = _run(["systemctl", "enable", "--now", _linux_service_name()])
            if enabled.returncode != 0:
                raise RuntimeError((enabled.stderr or enabled.stdout or "failed to enable systemd system service").strip())
            return {
                "ok": True,
                "platform": "linux",
                "scope": "system",
                "run_as": run_as_user or "root",
                "wrapper": str(wrapper),
                "service": str(system_service_path),
                "user_service": str(user_service_path),
            }

        if scope == "user" and shutil_which("systemctl") and not _linux_systemctl_user_available():
            raise RuntimeError(_linux_systemctl_user_unavailable_hint(user_service_path=user_service_path, wrapper=wrapper))

        if scope == "system" and shutil_which("systemctl") and not _linux_systemctl_system_available():
            raise RuntimeError(_linux_systemctl_system_unavailable_hint(wrapper=wrapper))

        if scope == "auto" and shutil_which("systemctl") and (not _linux_systemctl_user_available()) and (not _linux_systemctl_system_available()):
            raise RuntimeError(
                "\n".join(
                    [
                        "cannot enable autostart: neither systemctl --user nor systemctl (system) is available.",
                        "",
                        _linux_systemctl_user_unavailable_hint(user_service_path=user_service_path, wrapper=wrapper),
                        "",
                        _linux_systemctl_system_unavailable_hint(wrapper=wrapper),
                    ]
                ).strip()
            )

        return {"ok": True, "platform": "linux", "wrapper": str(wrapper), "service": str(user_service_path)}

    if _is_macos():
        wrapper = (bin_dir / "adaos-autostart.sh").resolve()
        _write_wrapper_sh(wrapper, argv=spec.argv, env=spec.env)
        agent_dir = (_home() / "Library" / "LaunchAgents").resolve()
        plist_path = (agent_dir / f"{_macos_label()}.plist").resolve()
        logs_dir = ctx.paths.logs_dir()
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = (logs_dir / "autostart.out.log").resolve()
        stderr_path = (logs_dir / "autostart.err.log").resolve()
        plist = "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
                '<plist version="1.0">',
                "<dict>",
                "  <key>Label</key>",
                f"  <string>{_macos_label()}</string>",
                "  <key>ProgramArguments</key>",
                "  <array>",
                "    <string>/bin/bash</string>",
                "    <string>-lc</string>",
                f"    <string>{wrapper}</string>",
                "  </array>",
                "  <key>RunAtLoad</key>",
                "  <true/>",
                "  <key>KeepAlive</key>",
                "  <true/>",
                "  <key>StandardOutPath</key>",
                f"  <string>{stdout_path}</string>",
                "  <key>StandardErrorPath</key>",
                f"  <string>{stderr_path}</string>",
                "</dict>",
                "</plist>",
                "",
            ]
        )
        _write_text(plist_path, plist)

        if shutil_which("launchctl"):
            uid = str(os.getuid()) if hasattr(os, "getuid") else ""
            domain = f"gui/{uid}" if uid else "gui"
            _run(["launchctl", "bootout", domain, str(plist_path)])
            boot = _run(["launchctl", "bootstrap", domain, str(plist_path)])
            if boot.returncode != 0:
                legacy = _run(["launchctl", "load", "-w", str(plist_path)])
                if legacy.returncode != 0:
                    raise RuntimeError((legacy.stderr or legacy.stdout or boot.stderr or boot.stdout or "failed to enable launchd agent").strip())

        return {"ok": True, "platform": "macos", "wrapper": str(wrapper), "plist": str(plist_path)}

    raise RuntimeError(f"autostart is not supported on platform: {platform.platform()}")


def disable(ctx: AgentContext) -> dict:
    base_dir = ctx.paths.base_dir()
    bin_dir = (base_dir / "bin").resolve()

    if _is_windows():
        name = _windows_task_name()
        proc = _run(["schtasks", "/Delete", "/F", "/TN", name])
        ok = proc.returncode == 0
        wrapper = (bin_dir / "adaos-autostart.ps1").resolve()
        if wrapper.exists():
            try:
                wrapper.unlink()
            except Exception:
                pass
        return {"ok": ok, "platform": "windows", "task": name}

    if _is_linux():
        user_service_path = _linux_service_path_user()
        system_service_path = _linux_service_path_system()

        if shutil_which("systemctl") and _linux_systemctl_user_available():
            _run(["systemctl", "--user", "disable", "--now", _linux_service_name()])
            _run(["systemctl", "--user", "daemon-reload"])
        if shutil_which("systemctl") and _linux_is_root() and _linux_has_systemd_pid1():
            _run(["systemctl", "disable", "--now", _linux_service_name()])
            _run(["systemctl", "daemon-reload"])

        if user_service_path.exists():
            try:
                user_service_path.unlink()
            except Exception:
                pass
        if system_service_path.exists():
            try:
                system_service_path.unlink()
            except Exception:
                pass
        wrapper = (bin_dir / "adaos-autostart.sh").resolve()
        if wrapper.exists():
            try:
                wrapper.unlink()
            except Exception:
                pass
        return {
            "ok": True,
            "platform": "linux",
            "user_service": str(user_service_path),
            "system_service": str(system_service_path),
        }

    if _is_macos():
        plist_path = (_home() / "Library" / "LaunchAgents" / f"{_macos_label()}.plist").resolve()
        if shutil_which("launchctl"):
            uid = str(os.getuid()) if hasattr(os, "getuid") else ""
            domain = f"gui/{uid}" if uid else "gui"
            _run(["launchctl", "bootout", domain, str(plist_path)])
            _run(["launchctl", "unload", "-w", str(plist_path)])
        if plist_path.exists():
            try:
                plist_path.unlink()
            except Exception:
                pass
        wrapper = (bin_dir / "adaos-autostart.sh").resolve()
        if wrapper.exists():
            try:
                wrapper.unlink()
            except Exception:
                pass
        return {"ok": True, "platform": "macos", "plist": str(plist_path)}

    raise RuntimeError(f"autostart is not supported on platform: {platform.platform()}")


def status(ctx: AgentContext) -> dict:
    base_dir = ctx.paths.base_dir()
    bin_dir = (base_dir / "bin").resolve()

    if _is_windows():
        name = _windows_task_name()
        proc = _run(["schtasks", "/Query", "/TN", name, "/V", "/FO", "LIST"])
        task_info = _parse_windows_task_info(proc.stdout or "")
        expected_wrapper = (bin_dir / "adaos-autostart.ps1").resolve()
        task_to_run = task_info.get("task to run") or ""
        registered_wrapper_raw = _extract_task_wrapper_from_command(task_to_run)
        registered_wrapper = Path(registered_wrapper_raw).expanduser().resolve() if registered_wrapper_raw else None
        wrapper = registered_wrapper or expected_wrapper
        wrapper_env = _parse_wrapper_env(wrapper) if wrapper.exists() else {}
        state_raw = (task_info.get("scheduled task state") or task_info.get("status") or "").strip().lower()
        enabled = proc.returncode == 0 and state_raw not in {"disabled"}
        active = proc.returncode == 0 and state_raw in {"running"}
        host_port = _parse_wrapper_host_port(wrapper) if wrapper.exists() else None
        configured_host, configured_port = host_port or ("127.0.0.1", 8777)
        live_host_port = _discover_live_control_bind(configured_host, configured_port) if active else None
        host, port = live_host_port or (configured_host, configured_port)
        listening = bool(live_host_port) if active else False
        payload = {
            "platform": "windows",
            "enabled": enabled,
            "active": active,
            "listening": listening,
            "host": host,
            "port": port,
            "url": f"http://{host}:{int(port)}",
            "task": name,
            "wrapper": str(wrapper),
            "expected_wrapper": str(expected_wrapper),
        }
        if wrapper_env:
            payload["wrapper_env"] = wrapper_env
            wrapper_base_dir = str(wrapper_env.get("ADAOS_BASE_DIR") or "").strip()
            if wrapper_base_dir:
                payload["base_dir"] = str(Path(wrapper_base_dir).expanduser().resolve())
            wrapper_shared_dotenv = str(wrapper_env.get("ADAOS_SHARED_DOTENV_PATH") or "").strip()
            if wrapper_shared_dotenv:
                payload["shared_dotenv_path"] = str(Path(wrapper_shared_dotenv).expanduser().resolve())
            supervisor_host = str(wrapper_env.get("ADAOS_SUPERVISOR_HOST") or "").strip()
            supervisor_port = str(wrapper_env.get("ADAOS_SUPERVISOR_PORT") or "").strip()
            if supervisor_port:
                payload["supervisor_url"] = f"http://{supervisor_host or '127.0.0.1'}:{supervisor_port}"
        core_update_status = _core_update_status_from_base_dir(payload.get("base_dir") or ctx.paths.base_dir())
        if core_update_status:
            payload["core_update_status"] = core_update_status
        if (configured_host, configured_port) != (host, port):
            payload["configured_host"] = configured_host
            payload["configured_port"] = configured_port
            payload["configured_url"] = f"http://{configured_host}:{int(configured_port)}"
            payload["live_url"] = f"http://{host}:{int(port)}"
        if task_to_run:
            payload["task_to_run"] = task_to_run
        if registered_wrapper is not None:
            payload["registered_wrapper"] = str(registered_wrapper)
            payload["wrapper_matches_expected"] = registered_wrapper == expected_wrapper
        if state_raw:
            payload["task_state"] = state_raw
        return payload

    if _is_linux():
        user_service_path = _linux_service_path_user()
        system_service_path = _linux_service_path_system()

        def _parse_execstart(path: Path) -> Path | None:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                return None
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("ExecStart="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if not value:
                        return None
                    try:
                        return Path(value).expanduser().resolve()
                    except Exception:
                        return None
            return None

        def _query_user() -> dict[str, object]:
            enabled = user_service_path.exists()
            active = None
            if shutil_which("systemctl") and _linux_systemctl_user_available():
                is_enabled = _run(["systemctl", "--user", "is-enabled", _linux_service_name()])
                enabled = is_enabled.returncode == 0
                is_active = _run(["systemctl", "--user", "is-active", _linux_service_name()])
                active = is_active.returncode == 0
            return {"scope": "user", "enabled": bool(enabled), "active": active, "service_path": user_service_path}

        def _query_system() -> dict[str, object]:
            enabled = system_service_path.exists()
            active = None
            if shutil_which("systemctl") and _linux_is_root() and _linux_has_systemd_pid1():
                is_enabled = _run(["systemctl", "is-enabled", _linux_service_name()])
                enabled = is_enabled.returncode == 0
                is_active = _run(["systemctl", "is-active", _linux_service_name()])
                active = is_active.returncode == 0
            return {"scope": "system", "enabled": bool(enabled), "active": active, "service_path": system_service_path}

        candidates: list[dict[str, object]] = []
        if _linux_is_root() and _linux_has_systemd_pid1():
            candidates.append(_query_system())
        if _linux_systemctl_user_available() or user_service_path.exists():
            candidates.append(_query_user())
        if not candidates:
            candidates.append(_query_user())

        def _candidate_rank(item: dict[str, object]) -> tuple[bool, bool, bool]:
            return (
                item.get("active") is True,
                item.get("enabled") is True,
                item.get("scope") == "system" and _linux_is_root() and _linux_has_systemd_pid1(),
            )

        selected = max(candidates, key=_candidate_rank)
        scope = str(selected.get("scope") or "user")
        enabled = bool(selected.get("enabled"))
        active = selected.get("active")
        service_path = Path(selected.get("service_path") or user_service_path)

        wrapper = (bin_dir / "adaos-autostart.sh").resolve()
        wrapper_from_service = _parse_execstart(service_path) if service_path.exists() else None
        if wrapper_from_service is not None:
            wrapper = wrapper_from_service
        wrapper_env = _parse_wrapper_env(wrapper) if wrapper.exists() else {}

        host_port = _parse_wrapper_host_port(wrapper) if wrapper.exists() else None
        configured_host, configured_port = host_port or ("127.0.0.1", 8777)
        live_host_port = _discover_live_control_bind(configured_host, configured_port) if active else None
        host, port = live_host_port or (configured_host, configured_port)
        listening = bool(live_host_port) if active else False
        payload = {
            "platform": "linux",
            "scope": scope,
            "enabled": bool(enabled),
            "active": active,
            "listening": listening,
            "host": host,
            "port": port,
            "url": f"http://{host}:{int(port)}",
            "service": str(service_path),
            "user_service": str(user_service_path),
            "system_service": str(system_service_path),
            "wrapper": str(wrapper),
        }
        main_pid = _linux_service_main_pid(scope) if active else None
        if main_pid is not None:
            payload["service_main_pid"] = main_pid
        if wrapper_env:
            payload["wrapper_env"] = wrapper_env
            wrapper_base_dir = str(wrapper_env.get("ADAOS_BASE_DIR") or "").strip()
            if wrapper_base_dir:
                payload["base_dir"] = str(Path(wrapper_base_dir).expanduser().resolve())
            wrapper_shared_dotenv = str(wrapper_env.get("ADAOS_SHARED_DOTENV_PATH") or "").strip()
            if wrapper_shared_dotenv:
                payload["shared_dotenv_path"] = str(Path(wrapper_shared_dotenv).expanduser().resolve())
            supervisor_host = str(wrapper_env.get("ADAOS_SUPERVISOR_HOST") or "").strip()
            supervisor_port = str(wrapper_env.get("ADAOS_SUPERVISOR_PORT") or "").strip()
            if supervisor_port:
                payload["supervisor_url"] = f"http://{supervisor_host or '127.0.0.1'}:{supervisor_port}"
        payload["user_service_exists"] = user_service_path.exists()
        payload["system_service_exists"] = system_service_path.exists()
        payload["system_scope_preferred"] = _linux_should_prefer_system_scope("auto")
        core_update_status = _core_update_status_from_base_dir(payload.get("base_dir") or ctx.paths.base_dir())
        if core_update_status:
            payload["core_update_status"] = core_update_status
        if (configured_host, configured_port) != (host, port):
            payload["configured_host"] = configured_host
            payload["configured_port"] = configured_port
            payload["configured_url"] = f"http://{configured_host}:{int(configured_port)}"
            payload["live_url"] = f"http://{host}:{int(port)}"
        return payload

    if _is_macos():
        plist_path = (_home() / "Library" / "LaunchAgents" / f"{_macos_label()}.plist").resolve()
        enabled = plist_path.exists()
        active = None
        if shutil_which("launchctl") and enabled:
            uid = str(os.getuid()) if hasattr(os, "getuid") else ""
            domain = f"gui/{uid}" if uid else "gui"
            probe = _run(["launchctl", "print", f"{domain}/{_macos_label()}"])
            active = probe.returncode == 0
        wrapper = (bin_dir / "adaos-autostart.sh").resolve()
        wrapper_env = _parse_wrapper_env(wrapper) if wrapper.exists() else {}
        host_port = _parse_wrapper_host_port(wrapper) if wrapper.exists() else None
        configured_host, configured_port = host_port or ("127.0.0.1", 8777)
        live_host_port = _discover_live_control_bind(configured_host, configured_port) if active else None
        host, port = live_host_port or (configured_host, configured_port)
        listening = bool(live_host_port) if active else False
        payload = {
            "platform": "macos",
            "enabled": bool(enabled),
            "active": active,
            "listening": listening,
            "host": host,
            "port": port,
            "url": f"http://{host}:{int(port)}",
            "plist": str(plist_path),
            "wrapper": str(wrapper),
        }
        if wrapper_env:
            payload["wrapper_env"] = wrapper_env
            wrapper_base_dir = str(wrapper_env.get("ADAOS_BASE_DIR") or "").strip()
            if wrapper_base_dir:
                payload["base_dir"] = str(Path(wrapper_base_dir).expanduser().resolve())
            wrapper_shared_dotenv = str(wrapper_env.get("ADAOS_SHARED_DOTENV_PATH") or "").strip()
            if wrapper_shared_dotenv:
                payload["shared_dotenv_path"] = str(Path(wrapper_shared_dotenv).expanduser().resolve())
            supervisor_host = str(wrapper_env.get("ADAOS_SUPERVISOR_HOST") or "").strip()
            supervisor_port = str(wrapper_env.get("ADAOS_SUPERVISOR_PORT") or "").strip()
            if supervisor_port:
                payload["supervisor_url"] = f"http://{supervisor_host or '127.0.0.1'}:{supervisor_port}"
        core_update_status = _core_update_status_from_base_dir(payload.get("base_dir") or ctx.paths.base_dir())
        if core_update_status:
            payload["core_update_status"] = core_update_status
        if (configured_host, configured_port) != (host, port):
            payload["configured_host"] = configured_host
            payload["configured_port"] = configured_port
            payload["configured_url"] = f"http://{configured_host}:{int(configured_port)}"
            payload["live_url"] = f"http://{host}:{int(port)}"
        return payload

    return {"platform": platform.platform(), "enabled": False}


def shutil_which(cmd: str) -> str | None:
    import shutil

    return shutil.which(cmd)
