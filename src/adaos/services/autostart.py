from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from adaos.services.agent_context import AgentContext


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


def default_spec(
    ctx: AgentContext,
    *,
    host: str = "127.0.0.1",
    port: int = 8777,
    token: str | None = None,
) -> AutostartSpec:
    base_dir = ctx.paths.base_dir()
    profile = getattr(ctx.settings, "profile", "default")
    argv = (
        sys.executable,
        "-m",
        "adaos",
        "--base-dir",
        str(base_dir),
        "--profile",
        str(profile),
        "api",
        "serve",
        "--host",
        host,
        "--port",
        str(int(port)),
    )
    env = {
        "ADAOS_BASE_DIR": str(base_dir),
        "ADAOS_PROFILE": str(profile),
    }
    if token:
        env["ADAOS_TOKEN"] = token
    return AutostartSpec(name="adaos", argv=argv, env=env)


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


def _windows_task_name() -> str:
    return "AdaOS"


def _linux_service_name() -> str:
    return "adaos.service"


def _macos_label() -> str:
    return "com.adaos.autostart"


def enable(ctx: AgentContext, spec: AutostartSpec, *, force: bool = True) -> dict:
    base_dir = ctx.paths.base_dir()
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
        wrapper = (bin_dir / "adaos-autostart.sh").resolve()
        _write_wrapper_sh(wrapper, argv=spec.argv, env=spec.env)
        service_dir = (_home() / ".config" / "systemd" / "user").resolve()
        service_path = (service_dir / _linux_service_name()).resolve()
        _write_text(
            service_path,
            "\n".join(
                [
                    "[Unit]",
                    "Description=AdaOS (user)",
                    "After=network-online.target",
                    "",
                    "[Service]",
                    "Type=simple",
                    f"ExecStart={wrapper}",
                    "Restart=on-failure",
                    "RestartSec=3",
                    "Environment=PYTHONUNBUFFERED=1",
                    "",
                    "[Install]",
                    "WantedBy=default.target",
                    "",
                ]
            ),
        )
        if shutil_which("systemctl"):
            _run(["systemctl", "--user", "daemon-reload"])
            enabled = _run(["systemctl", "--user", "enable", "--now", _linux_service_name()])
            if enabled.returncode != 0:
                raise RuntimeError((enabled.stderr or enabled.stdout or "failed to enable systemd user service").strip())
        return {"ok": True, "platform": "linux", "wrapper": str(wrapper), "service": str(service_path)}

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
        service_path = (_home() / ".config" / "systemd" / "user" / _linux_service_name()).resolve()
        if shutil_which("systemctl"):
            _run(["systemctl", "--user", "disable", "--now", _linux_service_name()])
            _run(["systemctl", "--user", "daemon-reload"])
        if service_path.exists():
            try:
                service_path.unlink()
            except Exception:
                pass
        wrapper = (bin_dir / "adaos-autostart.sh").resolve()
        if wrapper.exists():
            try:
                wrapper.unlink()
            except Exception:
                pass
        return {"ok": True, "platform": "linux", "service": str(service_path)}

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
        proc = _run(["schtasks", "/Query", "/TN", name])
        wrapper = (bin_dir / "adaos-autostart.ps1").resolve()
        return {"platform": "windows", "enabled": proc.returncode == 0, "task": name, "wrapper": str(wrapper)}

    if _is_linux():
        service_path = (_home() / ".config" / "systemd" / "user" / _linux_service_name()).resolve()
        enabled = service_path.exists()
        active = None
        if shutil_which("systemctl"):
            is_enabled = _run(["systemctl", "--user", "is-enabled", _linux_service_name()])
            enabled = is_enabled.returncode == 0
            is_active = _run(["systemctl", "--user", "is-active", _linux_service_name()])
            active = is_active.returncode == 0
        wrapper = (bin_dir / "adaos-autostart.sh").resolve()
        return {
            "platform": "linux",
            "enabled": bool(enabled),
            "active": active,
            "service": str(service_path),
            "wrapper": str(wrapper),
        }

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
        return {
            "platform": "macos",
            "enabled": bool(enabled),
            "active": active,
            "plist": str(plist_path),
            "wrapper": str(wrapper),
        }

    return {"platform": platform.platform(), "enabled": False}


def shutil_which(cmd: str) -> str | None:
    import shutil

    return shutil.which(cmd)
