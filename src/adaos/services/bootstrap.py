# src\adaos\services\bootstrap.py
from __future__ import annotations

import asyncio
import base64
import json as _json
import logging
import os
import socket
import time
import traceback
import uuid
from collections import deque
from pathlib import Path
from typing import Any, List, Optional, Sequence

import nats as _nats

from adaos.adapters.db.sqlite_schema import ensure_schema
from adaos.adapters.scenarios.git_repo import GitScenarioRepository
from adaos.adapters.skills.git_repo import GitSkillRepository
from adaos.domain import Event
from adaos.ports.heartbeat import HeartbeatPort
from adaos.ports.skills_loader import SkillsLoaderPort
from adaos.ports.subnet_registry import SubnetRegistryPort
from adaos.sdk.core.decorators import register_subscriptions
from adaos.sdk.data import bus
from adaos.services import yjs as _y_store  # ensure YStore subscriptions are registered
from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.chat_io import telemetry as tm
from adaos.services.chat_io.interfaces import ChatOutputEvent, ChatOutputMessage
from adaos.services.chat_io.nlu_bridge import register_chat_nlu_bridge  # chat->NLU bridge
from adaos.services.eventbus import LocalEventBus
from adaos.services.io_bus.http_fallback import HttpFallbackBus
from adaos.services.io_bus.local_bus import LocalIoBus
from adaos.services.node_config import NodeConfig, load_config, set_role as cfg_set_role
from adaos.services.scheduler import start_scheduler
from adaos.services.scenario import (
    webspace_runtime as _scenario_ws_runtime,  # ensure core scenario subscriptions
)
from adaos.services.scenario import workflow_runtime as _scenario_workflow_runtime  # ensure scenario workflow subscriptions
from adaos.services import weather as _weather_services  # ensure weather observers
from adaos.services import nlu as _nlu_services  # ensure NLU dispatcher subscriptions
from adaos.services.skill import service_supervisor_runtime as _service_supervisor_runtime  # ensure service supervisor subscriptions
from adaos.services.skill.service_supervisor import get_service_supervisor
from adaos.integrations.telegram.sender import TelegramSender


class BootstrapService:
    def __init__(
        self,
        ctx: AgentContext,
        *,
        heartbeat: HeartbeatPort,
        skills_loader: SkillsLoaderPort,
        subnet_registry: SubnetRegistryPort,
    ) -> None:
        self.ctx = ctx
        self.heartbeat = heartbeat
        self.skills_loader = skills_loader
        self.subnet_registry = subnet_registry
        self._boot_tasks: List[asyncio.Task] = []
        self._ready = asyncio.Event()
        self._booted = False
        self._app: Any = None
        self._io_bus: Any = None
        self._log = logging.getLogger("adaos.hub-io")

    def is_ready(self) -> bool:
        return self._ready.is_set()

    def _prepare_environment(self) -> None:
        """
        Гарантированная подготовка окружения:
          - создаёт каталоги (skills, scenarios, state, cache, logs)
          - инициализирует схему БД (skills/scenarios)
          - при наличии URL монорепо — клонирует репозитории без установки
        """
        ctx = self.ctx

        # каталоги (учитываем, что в paths могут быть callables)
        def _resolve(x):
            return x() if callable(x) else x

        skills_root = Path(_resolve(getattr(ctx.paths, "skills_dir", "")))
        scenarios_root = Path(_resolve(getattr(ctx.paths, "scenarios_dir", "")))
        state_root = Path(_resolve(getattr(ctx.paths, "state_dir", "")))
        cache_root = Path(_resolve(getattr(ctx.paths, "cache_dir", "")))
        logs_root = Path(_resolve(getattr(ctx.paths, "logs_dir", "")))

        for p in (skills_root, scenarios_root, state_root, cache_root, logs_root):
            if p:
                p.mkdir(parents=True, exist_ok=True)

        # схема БД (единая функция, не через побочный эффект конкретного реестра)
        ensure_schema(ctx.sql)

        # в тестах — не трогаем удалённые репозитории/сеть
        if os.getenv("ADAOS_TESTING") == "1":
            return

        # Default routing rules for RouterService (stdout + telegram broadcast).
        # This file is a runtime config (often ignored by git) but must exist for
        # system notifications (subnet.started/stopped, greet_on_boot, etc).
        try:
            base_dir = getattr(ctx.paths, "base_dir", None)
            base_dir = base_dir() if callable(base_dir) else base_dir
            if base_dir:
                rules_path = Path(base_dir) / "route_rules.yaml"
                if not rules_path.exists():
                    rules_path.write_text(
                        "rules:\n"
                        "  - priority: 60\n"
                        "    match: {}\n"
                        "    target: {node_id: this, kind: io_type, io_type: stdout}\n"
                        "  - priority: 50\n"
                        "    match: {}\n"
                        "    target: {node_id: this, kind: io_type, io_type: telegram}\n",
                        encoding="utf-8",
                    )
        except Exception:
            pass

        # монорепо навыков
        try:
            if ctx.settings.skills_monorepo_url and not (skills_root / ".git").exists():
                GitSkillRepository(
                    paths=ctx.paths,
                    git=ctx.git,
                    monorepo_url=getattr(ctx.settings, "skills_monorepo_url", None),
                    monorepo_branch=getattr(ctx.settings, "skills_monorepo_branch", None),
                ).ensure()
        except Exception:
            # не блокируем бут при сбое ensure; логирование можно добавить позже
            pass

        # монорепо сценариев (поддержим оба возможных конструктора)
        try:
            if ctx.settings.scenarios_monorepo_url and not (scenarios_root / ".git").exists():
                GitScenarioRepository(
                    paths=ctx.paths,
                    git=ctx.git,
                    url=getattr(ctx.settings, "scenarios_monorepo_url", None),
                    branch=getattr(ctx.settings, "scenarios_monorepo_branch", None),
                ).ensure()

        except Exception:
            pass

    async def _member_register_and_heartbeat(self, conf: NodeConfig) -> Optional[asyncio.Task]:
        ok = await self.heartbeat.register(conf.hub_url or "", conf.token or "", node_id=conf.node_id, subnet_id=conf.subnet_id, hostname=socket.gethostname(), roles=["member"])
        if not ok:
            await bus.emit("net.subnet.register.error", {"status": "non-200"}, source="lifecycle", actor="system")
            return None
        await bus.emit("net.subnet.registered", {"hub": conf.hub_url}, source="lifecycle", actor="system")

        async def loop() -> None:
            backoff = 1
            while True:
                try:
                    ok_hb = await self.heartbeat.heartbeat(conf.hub_url or "", conf.token or "", node_id=conf.node_id)
                    if ok_hb:
                        backoff = 1
                    else:
                        await bus.emit("net.subnet.heartbeat.warn", {"status": "non-200"}, source="lifecycle", actor="system")
                        backoff = min(backoff * 2, 30)
                except Exception as e:
                    await bus.emit("net.subnet.heartbeat.error", {"error": str(e)}, source="lifecycle", actor="system")
                    backoff = min(backoff * 2, 30)
                await asyncio.sleep(backoff if backoff > 1 else 5)

        return asyncio.create_task(loop(), name="adaos-heartbeat")

    async def run_boot_sequence(self, app: Any) -> None:
        if self._booted:
            return
        self._app = app
        conf = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
        self._prepare_environment()
        # local adapter over LocalEventBus
        core_bus = self.ctx.bus if isinstance(self.ctx.bus, LocalEventBus) else LocalEventBus()
        io_bus: Any = LocalIoBus(core=core_bus)
        await io_bus.connect()
        print("[bootstrap] IO bus: LocalEventBus")
        self._io_bus = io_bus
        # Attach chat IO -> NLU bridge (e.g. Telegram text -> nlp.intent.detect.request)
        try:
            register_chat_nlu_bridge(core_bus)
        except Exception:
            self._log.warning("failed to register chat_io NLU bridge", exc_info=True)
        # expose in app.state
        try:
            setattr(app.state, "bus", io_bus)
        except Exception:
            pass
        await bus.emit("sys.boot.start", {"role": conf.role, "node_id": conf.node_id, "subnet_id": conf.subnet_id}, source="lifecycle", actor="system")
        await self.skills_loader.import_all_handlers(self.ctx.paths.skills_dir())
        # Start service-type skills (external processes).
        try:
            await get_service_supervisor().start_all()
        except Exception:
            self._log.warning("failed to start service skills", exc_info=True)
        await register_subscriptions()
        await bus.emit("sys.bus.ready", {}, source="lifecycle", actor="system")
        # Start in-process scheduler after the bus is ready.
        try:
            await start_scheduler()
        except Exception:
            self._log.warning("failed to start scheduler", exc_info=True)

        # Optional: monitor asyncio event loop lag to catch blocking handlers (which can manifest as
        # WebSocket stalls/timeouts and cascading disconnects).
        try:
            if os.getenv("ADAOS_LOOP_LAG_MONITOR", "0") == "1":
                try:
                    interval_s = float(os.getenv("ADAOS_LOOP_LAG_INTERVAL_S", "0.5") or "0.5")
                except Exception:
                    interval_s = 0.5
                if interval_s < 0.05:
                    interval_s = 0.05
                try:
                    warn_ms = float(os.getenv("ADAOS_LOOP_LAG_WARN_MS", "250") or "250")
                except Exception:
                    warn_ms = 250.0
                try:
                    dump_ms = float(os.getenv("ADAOS_LOOP_LAG_DUMP_MS", "2000") or "2000")
                except Exception:
                    dump_ms = 2000.0
                try:
                    dump_top = int(os.getenv("ADAOS_LOOP_LAG_DUMP_TOP", "10") or "10")
                except Exception:
                    dump_top = 10
                if dump_top < 1:
                    dump_top = 1
                if dump_top > 50:
                    dump_top = 50

                async def _loop_lag_monitor() -> None:
                    # Measure *per-interval* overshoot (do not accumulate drift), so we can distinguish
                    # a single stall from a slow-but-steady loop.
                    last_tick = time.monotonic()
                    last_log = 0.0
                    last_dump = 0.0
                    while True:
                        await asyncio.sleep(interval_s)
                        now = time.monotonic()
                        drift_s = (now - last_tick) - interval_s
                        last_tick = now
                        if drift_s < 0:
                            drift_s = 0.0
                        drift_ms = drift_s * 1000.0
                        if drift_ms >= warn_ms:
                            try:
                                # Local rate-limit (do not depend on hub-io _rl_log).
                                if now - last_log >= 1.0:
                                    last_log = now
                                    print(
                                        f"[diag] event loop lag {drift_ms:.0f}ms (interval={interval_s:.2f}s warn={warn_ms:.0f}ms dump={dump_ms:.0f}ms)"
                                    )
                            except Exception:
                                pass
                        if drift_ms >= dump_ms and (now - last_dump) >= max(5.0, interval_s):
                            last_dump = now
                            try:
                                tasks = list(asyncio.all_tasks())
                                # Keep deterministic ordering for repeated dumps.
                                tasks.sort(key=lambda t: (0 if t is asyncio.current_task() else 1, t.get_name()))
                                lines: list[str] = []
                                for t in tasks[:dump_top]:
                                    try:
                                        frames = t.get_stack(limit=1)
                                        top = frames[-1] if frames else None
                                        loc = None
                                        if top is not None:
                                            try:
                                                loc = f"{top.f_code.co_filename}:{top.f_lineno}"
                                            except Exception:
                                                loc = None
                                        lines.append(f"- task={t.get_name()} done={t.done()} cancelled={t.cancelled()} at={loc}")
                                    except Exception:
                                        continue
                                if lines:
                                    print("[diag] loop lag dump:\n" + "\n".join(lines))
                            except Exception:
                                pass

                self._boot_tasks.append(asyncio.create_task(_loop_lag_monitor(), name="adaos-loop-lag-monitor"))
        except Exception:
            pass

        # Optional: hang watchdog (thread-based) to capture the main thread stack during prolonged
        # event loop stalls. This catches cases where asyncio tasks show "await" positions only.
        try:
            if os.getenv("ADAOS_LOOP_HANG_WATCHDOG", "0") == "1":
                try:
                    import threading as _threading
                    import sys as _sys
                    import traceback as _traceback
                except Exception:
                    _threading = None  # type: ignore[assignment]
                    _sys = None  # type: ignore[assignment]
                    _traceback = None  # type: ignore[assignment]
                if _threading and _sys and _traceback:
                    try:
                        hang_ms = float(os.getenv("ADAOS_LOOP_HANG_MS", "3000") or "3000")
                    except Exception:
                        hang_ms = 3000.0
                    try:
                        every_s = float(os.getenv("ADAOS_LOOP_HANG_EVERY_S", "10") or "10")
                    except Exception:
                        every_s = 10.0
                    try:
                        stack_limit = int(os.getenv("ADAOS_LOOP_HANG_STACK", "40") or "40")
                    except Exception:
                        stack_limit = 40
                    if stack_limit < 5:
                        stack_limit = 5
                    if stack_limit > 200:
                        stack_limit = 200
                    if hang_ms < 200:
                        hang_ms = 200.0
                    if every_s < 1:
                        every_s = 1.0

                    main_tid = _threading.get_ident()
                    last_tick_box = {"t": time.monotonic()}

                    async def _tick() -> None:
                        while True:
                            last_tick_box["t"] = time.monotonic()
                            await asyncio.sleep(0.2)

                    self._boot_tasks.append(asyncio.create_task(_tick(), name="adaos-loop-tick"))

                    def _watch() -> None:
                        last_dump = 0.0
                        while True:
                            time.sleep(0.25)
                            now = time.monotonic()
                            dt_ms = (now - float(last_tick_box.get("t", now))) * 1000.0
                            if dt_ms < hang_ms:
                                continue
                            if now - last_dump < every_s:
                                continue
                            last_dump = now
                            try:
                                fr = _sys._current_frames().get(main_tid)  # type: ignore[attr-defined]
                                if fr is None:
                                    print(f"[diag] event loop hang {dt_ms:.0f}ms (no frame)")
                                    continue
                                st = "".join(_traceback.format_stack(fr, limit=stack_limit))
                                print(f"[diag] event loop hang {dt_ms:.0f}ms stack:\n{st.rstrip()}")
                            except Exception:
                                continue

                    t = _threading.Thread(target=_watch, name="adaos-loop-hang-watchdog", daemon=True)
                    t.start()
        except Exception:
            pass
        if conf.role == "hub":
            await bus.emit("net.subnet.hub.ready", {"subnet_id": conf.subnet_id}, source="lifecycle", actor="system")

            async def lease_monitor() -> None:
                while True:
                    for info in self.subnet_registry.mark_down_if_expired():
                        await bus.emit("net.subnet.node.down", {"node_id": getattr(info, "node_id", None)}, source="lifecycle", actor="system")
                    await asyncio.sleep(5)

            self._boot_tasks.append(asyncio.create_task(lease_monitor(), name="adaos-lease-monitor"))
            self._ready.set()
            self._booted = True
            await bus.emit("sys.ready", {"ts": time.time()}, source="lifecycle", actor="system")
        else:
            task = await self._member_register_and_heartbeat(conf)
            if task:
                self._boot_tasks.append(task)
                self._ready.set()
                self._booted = True
                await bus.emit("sys.ready", {"ts": time.time()}, source="lifecycle", actor="system")

        # After IO bus is ready, wire outbound subscriber for Telegram if NATS/local
        try:
            if hasattr(self._io_bus, "subscribe_output"):

                # Subscribe to all bot ids ("tg.output.*") and use the single configured TG_BOT_TOKEN.
                sender = TelegramSender("any-bot")

                async def _handler(subject: str, data: bytes) -> None:
                    try:
                        payload = _json.loads(data.decode("utf-8"))
                        # payload may already match ChatOutputEvent schema
                        messages = [ChatOutputMessage(**m) for m in payload.get("messages", [])]
                        out = ChatOutputEvent(target=payload.get("target", {}), messages=messages, options=payload.get("options"))
                        await sender.send(out)
                        for m in messages:
                            tm.record_event("outbound_total", {"type": m.type})
                    except Exception as e:
                        # On error, emit DLQ if possible
                        try:
                            dlq_env = {"error": str(e), "subject": subject, "data": payload if "payload" in locals() else None}
                            if hasattr(self._io_bus, "publish_dlq"):
                                await self._io_bus.publish_dlq("output", dlq_env)
                        except Exception:
                            pass

                await self._io_bus.subscribe_output("*", _handler)
        except Exception:
            pass

        # Inbound bridge from root NATS -> local event bus (tg.input.<hub_id>)
        try:
            # Hot-reload friendly: read NATS config from node.yaml on every connect attempt.
            hub_id = (getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)).subnet_id
            if hub_id:
                try:
                    if os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                        print(f"[hub-io] nats init: hub_id={hub_id}")
                except Exception:
                    pass

                # Track connectivity state to log/emit only on transitions
                reported_down = False
                nats_last_log_at: dict[str, float] = {}
                nats_last_ok_at: float | None = None
                # Track flaky NATS WS endpoints and temporarily avoid them after short transient drops.
                nats_server_quarantine_until: dict[str, float] = {}
                nats_last_server: str | None = None

                def _rl_log(key: str, msg: str, *, every_s: float = 5.0) -> None:
                    """
                    Rate-limited console log helper for noisy NATS diagnostics.
                    Uses monotonic time to avoid being affected by clock changes.
                    """
                    try:
                        now = time.monotonic()
                        last = nats_last_log_at.get(key, 0.0)
                        if now - last < every_s:
                            return
                        nats_last_log_at[key] = now
                        print(msg)
                    except Exception:
                        return

                def _read_node_nats() -> tuple[str | None, str | None, str | None]:
                    try:
                        from adaos.services.capacity import _load_node_yaml as _load_node

                        nd = _load_node()
                        node_nats = (nd or {}).get("nats") if isinstance(nd, dict) else None
                        if not isinstance(node_nats, dict) or not node_nats:
                            return None, None, None
                        nurl = str(node_nats.get("ws_url") or "") or None
                        nuser = str(node_nats.get("user") or "") or None
                        npass = str(node_nats.get("pass") or "") or None
                        return nurl, nuser, npass
                    except Exception:
                        return None, None, None

                last_token_fetch = 0.0

                async def _fetch_nats_credentials() -> bool:
                    nonlocal last_token_fetch
                    # rate-limit attempts to avoid spamming root
                    now = time.monotonic()
                    if now - last_token_fetch < 30.0:
                        return False
                    last_token_fetch = now
                    debug = os.getenv("HUB_NATS_VERBOSE", "0") == "1"
                    try:
                        from adaos.services.root.client import RootHttpClient
                        from adaos.services.capacity import _load_node_yaml as _load_node, _save_node_yaml as _save_node
                        from adaos.services.node_config import load_config
                        from adaos.services.node_config import _expand_path as _expand_path
                    except Exception:
                        return False

                    try:
                        cfg = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
                    except Exception:
                        cfg = None

                    # Prefer node.yaml-driven mTLS materials (hub cert/key + CA) rather than Settings,
                    # because Settings may not include PKI fields.
                    base_url = getattr(self.ctx.settings, "api_base", None) or getattr(getattr(cfg, "root_settings", None), "base_url", None) or "https://api.inimatic.com"
                    try:
                        ca = _expand_path(getattr(getattr(cfg, "root_settings", None), "ca_cert", None), "keys/ca.cert")
                        cert = _expand_path(getattr(getattr(getattr(cfg, "subnet_settings", None), "hub", None), "cert", None), "keys/hub_cert.pem")
                        key = _expand_path(getattr(getattr(getattr(cfg, "subnet_settings", None), "hub", None), "key", None), "keys/hub_private.pem")
                    except Exception:
                        ca = None
                        cert = None
                        key = None

                    verify: Any = True
                    # By default keep system CA verification (important for public HTTPS like api.inimatic.com).
                    # If you need to pin CA explicitly, set ADAOS_ROOT_VERIFY_CA=1.
                    if os.getenv("ADAOS_ROOT_VERIFY_CA", "0") == "1" and ca is not None:
                        try:
                            if ca.exists():
                                verify = str(ca)
                        except Exception:
                            pass
                    cert_tuple = None
                    if cert is not None and key is not None:
                        try:
                            if cert.exists() and key.exists():
                                cert_tuple = (str(cert), str(key))
                        except Exception:
                            cert_tuple = None

                    client = RootHttpClient(base_url=str(base_url), verify=verify, cert=cert_tuple)
                    if not client.cert:
                        if debug:
                            try:
                                import logging as _logging

                                _logging.getLogger("adaos.hub_io").warning(
                                    "nats.mtls_missing",
                                    extra={
                                        "extra": {
                                            "base_url": str(base_url),
                                            "verify": str(verify),
                                            "ca_path": str(ca) if ca is not None else None,
                                            "cert_path": str(cert) if cert is not None else None,
                                            "key_path": str(key) if key is not None else None,
                                            "have_ca": bool(ca and ca.exists()),
                                            "have_cert": bool(cert and cert.exists()),
                                            "have_key": bool(key and key.exists()),
                                        }
                                    },
                                )
                            except Exception:
                                pass
                        return False

                    def _do_request() -> dict[str, Any] | None:
                        try:
                            data = client.request("POST", "/v1/hub/nats/token")
                            return dict(data) if isinstance(data, dict) else None
                        except Exception as e:
                            if debug:
                                try:
                                    import logging as _logging

                                    _logging.getLogger("adaos.hub_io").warning(
                                        "nats.token_request_failed",
                                        extra={
                                            "extra": {
                                                "base_url": str(base_url),
                                                "verify": str(verify),
                                                "error": str(e),
                                                "error_type": type(e).__name__,
                                            }
                                        },
                                    )
                                except Exception:
                                    pass
                            return None

                    data = await asyncio.to_thread(_do_request)
                    if not isinstance(data, dict):
                        return False
                    token = data.get("hub_nats_token")
                    nats_user = data.get("nats_user")
                    nats_ws_url = data.get("nats_ws_url")
                    # NATS WS is served via a dedicated hostname. Some deployments historically returned
                    # `wss://api.inimatic.com/nats` which results in a 400 during WS upgrade.
                    try:
                        if isinstance(nats_ws_url, str):
                            if nats_ws_url.startswith("wss://api.inimatic.com/nats"):
                                nats_ws_url = "wss://nats.inimatic.com/nats"
                            elif nats_ws_url == "wss://nats.inimatic.com":
                                nats_ws_url = "wss://nats.inimatic.com/nats"
                    except Exception:
                        pass
                    if not token or not nats_user or not nats_ws_url:
                        if debug:
                            try:
                                import logging as _logging

                                _logging.getLogger("adaos.hub_io").warning(
                                    "nats.token_response_incomplete",
                                    extra={"extra": {"data": data}},
                                )
                            except Exception:
                                pass
                        return False

                    try:
                        y = _load_node()
                        n = y.get("nats") or {}
                        if not isinstance(n, dict):
                            n = {}
                        n["ws_url"] = str(nats_ws_url)
                        n["user"] = str(nats_user)
                        n["pass"] = str(token)
                        y["nats"] = n
                        # If node.yaml is missing/minimal, seed core identity fields so other subsystems
                        # (Settings, tooling) can discover subnet/node info.
                        try:
                            if isinstance(cfg, object):
                                if "node_id" not in y:
                                    y["node_id"] = getattr(cfg, "node_id", None) or y.get("node_id")
                                if "subnet_id" not in y:
                                    y["subnet_id"] = getattr(cfg, "subnet_id", None) or y.get("subnet_id")
                                if "role" not in y:
                                    y["role"] = getattr(cfg, "role", None) or y.get("role")
                        except Exception:
                            pass
                        _save_node(y)
                        return True
                    except Exception:
                        return False

                # Correlate hub-side NATS WS sessions with root-side ws-nats-proxy logs + optionally snapshot root logs.
                ws_connect_tag: str | None = None
                last_root_snapshot_at: float | None = None

                async def _nats_bridge() -> None:
                    nonlocal reported_down
                    nonlocal nats_last_ok_at
                    nonlocal nats_last_server
                    backoff = 1.0
                    trace = os.getenv("HUB_NATS_TRACE", "0") == "1"
                    raw_keepalive_task: asyncio.Task | None = None
                    # Best-effort outbox for telegram replies when NATS is flapping.
                    try:
                        if not hasattr(self, "_tg_output_pending"):
                            setattr(self, "_tg_output_pending", deque())
                    except Exception:
                        pass
                    # nats-py WS transport (aiohttp) has historically surfaced WS CLOSE frames as `int` (close code),
                    # causing the NATS parser to crash with `TypeError: argument of type 'int' is not iterable` and
                    # leaving the connection in a half-dead state (read loop stopped, socket still open).
                    # Patch transport.readline() to ignore WS control frames and always return bytes.
                    try:
                        from nats.aio import transport as _nats_transport  # type: ignore
                        from nats.aio import client as _nats_client  # type: ignore
                        # nats-py 2.12.0 does not expose ProtocolError in nats.aio.errors; avoid import-time crashes.
                        # We only need an exception type for "cannot upgrade to TLS" (non-fatal diagnostic).
                        try:
                            from nats.aio.errors import NatsError as _NatsProtocolError  # type: ignore
                        except Exception:
                            _NatsProtocolError = RuntimeError  # type: ignore[assignment]
                        import aiohttp  # type: ignore

                        _ws_tr = getattr(_nats_transport, "WebSocketTransport", None)
                        _orig_rl = getattr(_ws_tr, "readline", None) if _ws_tr else None
                        _orig_write = getattr(_ws_tr, "write", None) if _ws_tr else None
                        _orig_writelines = getattr(_ws_tr, "writelines", None) if _ws_tr else None
                        _orig_drain = getattr(_ws_tr, "drain", None) if _ws_tr else None
                        _orig_connect = getattr(_ws_tr, "connect", None) if _ws_tr else None
                        _orig_connect_tls = getattr(_ws_tr, "connect_tls", None) if _ws_tr else None
                        _orig_at_eof = getattr(_ws_tr, "at_eof", None) if _ws_tr else None
                        _orig_process_ping = getattr(getattr(_nats_client, "Client", None), "_process_ping", None)
                        _orig_process_pong = getattr(getattr(_nats_client, "Client", None), "_process_pong", None)
                        patched_any = False
                        # Keep aiohttp WS autoping/autoclose enabled so WS-level PING/PONG works even when the NATS
                        # client is idle (otherwise a proxy WS ping can time out and force-close with 1006).
                        ws_safe_mode = False
                        # NATS-over-WS transport implementation used by nats-py.
                        #
                        # Why this exists:
                        # In some Windows environments we've observed aiohttp WS disconnecting with close 1006 and
                        # `Cannot write to closing transport` under sustained hub->root publishing (browser sees
                        # `hub_unreachable` / `yjs_sync_timeout`). The `websockets` library is more stable there.
                        #
                        # Values:
                        # - auto (default): prefer websockets on Windows if installed, else aiohttp
                        # - aiohttp: force aiohttp-based WebSocketTransport (nats-py default)
                        # - websockets: force websockets-based transport
                        try:
                            ws_impl = str(os.getenv("HUB_NATS_WS_IMPL", "auto") or "auto").strip().lower()
                        except Exception:
                            ws_impl = "auto"
                        use_ws_lib = False
                        websockets_mod = None
                        if ws_impl in ("websockets", "ws"):
                            use_ws_lib = True
                        elif ws_impl in ("aiohttp", "aio"):
                            use_ws_lib = False
                        else:
                            # auto
                            try:
                                use_ws_lib = os.name == "nt"
                            except Exception:
                                use_ws_lib = False
                        if use_ws_lib:
                            try:
                                import websockets as _websockets  # type: ignore

                                websockets_mod = _websockets
                            except Exception:
                                websockets_mod = None
                                use_ws_lib = False
                                if ws_impl in ("websockets", "ws"):
                                    try:
                                        _rl_log(
                                            "nats.ws_impl_fallback",
                                            "[hub-io] HUB_NATS_WS_IMPL=websockets requested but websockets is not installed; using aiohttp transport",
                                            every_s=3600.0,
                                        )
                                    except Exception:
                                        pass
                        try:
                            if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_TRACE", "0") == "1":
                                impl = "websockets" if (use_ws_lib and websockets_mod is not None) else "aiohttp"
                                _rl_log(
                                    "nats.ws_impl",
                                    f"[hub-io] nats ws impl: {impl} (HUB_NATS_WS_IMPL={ws_impl})",
                                    every_s=3600.0,
                                )
                        except Exception:
                            pass

                        def _ws_additional_headers(h: Any) -> Any:
                            # Convert aiohttp/multidict headers into websockets `additional_headers`.
                            if not h:
                                return None
                            try:
                                items = list(h.items())
                            except Exception:
                                try:
                                    items = list(dict(h).items())
                                except Exception:
                                    items = []
                            out: list[tuple[str, str]] = []
                            for k, v in items:
                                try:
                                    ks = str(k)
                                except Exception:
                                    continue
                                if isinstance(v, (list, tuple)):
                                    for vv in v:
                                        try:
                                            out.append((ks, str(vv)))
                                        except Exception:
                                            continue
                                else:
                                    try:
                                        out.append((ks, str(v)))
                                    except Exception:
                                        continue
                            return out or None
                        # aiohttp "heartbeat" sends WS PING frames and expects WS PONG frames.
                        # In our production ingress (wss://api.inimatic.com/nats, wss://nats.inimatic.com/nats),
                        # WS PONG delivery is not reliable (observed intermittent "No PONG received..." closes).
                        # Therefore we ignore HUB_NATS_WS_HEARTBEAT_S by default to avoid self-inflicted disconnects.
                        # Set HUB_NATS_WS_HEARTBEAT_FORCE=1 to override.
                        def _ws_heartbeat_from_env() -> float | None:
                            raw = None
                            try:
                                raw = os.getenv("HUB_NATS_WS_HEARTBEAT_S")
                                if raw is None:
                                    raw = os.getenv("HUB_NATS_WS_HEARTBEAT_DEFAULT_S")
                            except Exception:
                                raw = None
                            if raw is None:
                                return None
                            try:
                                s = str(raw).strip()
                            except Exception:
                                s = ""
                            if s == "":
                                return None
                            try:
                                v = float(s)
                            except Exception:
                                return None
                            hb = v if v > 0.0 else None
                            if hb is None:
                                return None
                            try:
                                force = str(os.getenv("HUB_NATS_WS_HEARTBEAT_FORCE", "0") or "").strip() == "1"
                            except Exception:
                                force = False
                            if force:
                                return hb
                            try:
                                _rl_log(
                                    "nats.ws_heartbeat_ignored",
                                    f"[hub-io] nats ws heartbeat ignored (HUB_NATS_WS_HEARTBEAT_S={s}); "
                                    f"WS PONG is unreliable on this ingress. "
                                    f"Set HUB_NATS_WS_HEARTBEAT_FORCE=1 to enable anyway.",
                                    every_s=3600.0,
                                )
                            except Exception:
                                pass
                            return None

                        # Allow updating the WS patch in long-running processes without full restarts.
                        # Old patches only set `_adaos_ws_patch`; newer ones also set `_adaos_ws_patch_v`.
                        PATCH_V = 2

                        def _needs_patch(fn: Any | None) -> bool:
                            try:
                                return int(getattr(fn, "_adaos_ws_patch_v", 0) or 0) < PATCH_V
                            except Exception:
                                return True

                        if _ws_tr is not None and callable(_orig_rl) and _needs_patch(_orig_rl):
                            async def _ws_readline_safe(self):  # type: ignore[no-redef]
                                # aiohttp surfaces WS control frames (PING/PONG/CLOSE) as WSMessage objects.
                                # nats-py expects bytes and will crash if we pass through e.g. close codes (int).
                                # Skip control frames and actively close on CLOSE to avoid half-dead sockets.
                                while True:
                                    ws_obj = getattr(self, "_ws", None)
                                    if ws_obj is None:
                                        return b""
                                    try:
                                        # `aiohttp`: ws.receive() -> WSMessage
                                        # `websockets`: ws.recv() -> bytes|str
                                        if callable(getattr(ws_obj, "recv", None)) and not callable(getattr(ws_obj, "receive", None)):
                                            raw = await ws_obj.recv()
                                            data = type("WSMsg", (), {})()
                                            setattr(data, "data", raw)
                                        else:
                                            data = await ws_obj.receive()
                                    except Exception as _rx_e:
                                        try:
                                            if os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                                ws_exc = None
                                                try:
                                                    exf = getattr(ws_obj, "exception", None)
                                                    if callable(exf):
                                                        ws_exc = exf()
                                                except Exception:
                                                    ws_exc = None
                                                ws_close_code = None
                                                ws_close_reason = None
                                                try:
                                                    ws_close_code = getattr(ws_obj, "close_code", None)
                                                except Exception:
                                                    ws_close_code = None
                                                try:
                                                    ws_close_reason = getattr(ws_obj, "close_reason", None)
                                                except Exception:
                                                    ws_close_reason = None
                                                _rl_log(
                                                    "nats.ws_receive_exc",
                                                    f"[hub-io] nats ws receive exception: err={type(_rx_e).__name__}: {_rx_e} ws_exc={ws_exc} close_code={ws_close_code} close_reason={ws_close_reason}",
                                                    every_s=1.0,
                                                )
                                        except Exception:
                                            pass
                                        return b""
                                    # Track last RX time for a higher-level watchdog.
                                    try:
                                        setattr(self, "_adaos_last_rx_at", time.monotonic())
                                    except Exception:
                                        pass
                                    try:
                                        t = getattr(data, "type", None)
                                        if t == aiohttp.WSMsgType.PING:
                                            try:
                                                if os.getenv("HUB_NATS_PING_TRACE", "0") == "1" or os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                                    b0 = getattr(data, "data", b"")
                                                    ln = len(b0) if isinstance(b0, (bytes, bytearray)) else 0
                                                    _rl_log("nats.ws_rx_ws_ping", f"[hub-io] nats ws rx WS PING len={ln} -> send WS PONG", every_s=1.0)
                                            except Exception:
                                                pass
                                            # Be defensive even if aiohttp autoping is enabled.
                                            try:
                                                await self._ws.pong(getattr(data, "data", b""))
                                            except Exception:
                                                pass
                                            continue
                                        if t == aiohttp.WSMsgType.PONG:
                                            try:
                                                if os.getenv("HUB_NATS_PING_TRACE", "0") == "1" or os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                                    b0 = getattr(data, "data", b"")
                                                    ln = len(b0) if isinstance(b0, (bytes, bytearray)) else 0
                                                    _rl_log("nats.ws_rx_ws_pong", f"[hub-io] nats ws rx WS PONG len={ln}", every_s=1.0)
                                            except Exception:
                                                pass
                                            continue
                                        if t in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                                            try:
                                                if os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                                    code = getattr(data, "data", None)
                                                    extra = getattr(data, "extra", None)
                                                    reason = None
                                                    try:
                                                        if isinstance(extra, (bytes, bytearray)):
                                                            reason = extra.decode("utf-8", errors="replace")
                                                        else:
                                                            reason = str(extra) if extra is not None else None
                                                    except Exception:
                                                        reason = None
                                                    ws_code = None
                                                    try:
                                                        ws_code = getattr(self._ws, "close_code", None)
                                                    except Exception:
                                                        ws_code = None
                                                    _rl_log(
                                                        "nats.ws_close_frame",
                                                        f"[hub-io] nats ws close frame: code={code} reason={reason} ws_close_code={ws_code}",
                                                        every_s=1.0,
                                                    )
                                            except Exception:
                                                pass
                                            try:
                                                await self._ws.close()
                                            except Exception:
                                                pass
                                            return b""
                                        if t in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                            try:
                                                if os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                                    exc = None
                                                    try:
                                                        exc = self._ws.exception()
                                                    except Exception:
                                                        exc = None
                                                    last_kind = None
                                                    last_subj = None
                                                    last_len = None
                                                    try:
                                                        last_kind = getattr(self, "_adaos_last_tx_kind", None)
                                                        last_subj = getattr(self, "_adaos_last_tx_subj", None)
                                                        last_len = getattr(self, "_adaos_last_tx_len", None)
                                                    except Exception:
                                                        last_kind = last_kind or None
                                                        last_subj = last_subj or None
                                                        last_len = last_len or None
                                                    _rl_log(
                                                        "nats.ws_closed",
                                                        f"[hub-io] nats ws closed/error: tag={getattr(self, '_adaos_ws_tag', None)} ws_url={getattr(self, '_adaos_ws_url', None)} type={t} ws_exc={exc} last_tx_kind={last_kind} last_tx_subj={last_subj} last_tx_len={last_len}",
                                                        every_s=1.0,
                                                    )
                                            except Exception:
                                                pass
                                            return b""
                                        d = getattr(data, "data", b"")
                                        if isinstance(d, (bytes, bytearray)):
                                            try:
                                                if os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                                    _rl_log(
                                                        "nats.ws_rx_any",
                                                        f"[hub-io] nats ws rx data len={len(d)}",
                                                        every_s=5.0,
                                                    )
                                                if os.getenv("HUB_NATS_PING_TRACE", "0") == "1" or os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                                    # Best-effort scan for NATS protocol keepalives (PING/PONG) without logging payloads.
                                                    # Use a small tail buffer to catch boundary-split sequences.
                                                    tail = getattr(self, "_adaos_nats_pp_tail", b"")
                                                    if not isinstance(tail, (bytes, bytearray)):
                                                        tail = b""
                                                    bb = bytes(d)
                                                    blob = bytes(tail) + bb
                                                    ping_n = blob.count(b"PING\r\n")
                                                    pong_n = blob.count(b"PONG\r\n")
                                                    try:
                                                        setattr(self, "_adaos_nats_pp_tail", blob[-5:])
                                                    except Exception:
                                                        pass
                                                    if ping_n or pong_n:
                                                        _rl_log(
                                                            "nats.ws_rx_nats_pp",
                                                            f"[hub-io] nats ws rx data ping={ping_n} pong={pong_n} len={len(bb)}",
                                                            every_s=1.0,
                                                        )
                                            except Exception:
                                                pass
                                            try:
                                                if os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                                    head = bytes(d[:512])
                                                    info_n = (1 if head.startswith(b"INFO ") else 0) + head.count(b"\nINFO ") + head.count(b"\r\nINFO ")
                                                    msg_n = (1 if head.startswith(b"MSG ") else 0) + head.count(b"\nMSG ") + head.count(b"\r\nMSG ")
                                                    err_n = 1 if b"-ERR" in head else 0
                                                    if info_n or msg_n or err_n:
                                                        _rl_log(
                                                            "nats.ws_rx_wiretap",
                                                            f"[hub-io] nats ws rx wiretap len={len(d)} info={info_n} msg={msg_n} err={err_n}",
                                                            every_s=1.0,
                                                        )
                                                    # Optional: extract and log a couple of subjects from MSG headers (no payload).
                                                    # This helps confirm whether root is sending `route.to_hub.*` while browser reports hub_unreachable.
                                                    if os.getenv("HUB_NATS_TRACE_SUBJECTS", "0") == "1" or os.getenv("HUB_ROUTE_VERBOSE", "0") == "1":
                                                        try:
                                                            # Look for "MSG <subject> ..." tokens.
                                                            # Data may contain multiple frames; only log the first match per chunk.
                                                            idx = head.find(b"MSG ")
                                                            if idx >= 0:
                                                                # require start or line boundary
                                                                if idx == 0 or head[idx - 1 : idx] in (b"\n", b"\r"):
                                                                    line_end = head.find(b"\n", idx)
                                                                    if line_end < 0:
                                                                        line_end = len(head)
                                                                    line = head[idx:line_end]
                                                                    # line: b"MSG <subj> <sid> [reply] <len>\\r"
                                                                    parts = line.split()
                                                                    subj = parts[1] if len(parts) >= 2 else b""
                                                                    if subj.startswith(b"route.to_hub.") or subj.startswith(b"tg.") or subj.startswith(b"hub."):
                                                                        _rl_log(
                                                                            "nats.ws_rx_subject",
                                                                            f"[hub-io] nats ws rx MSG subj={subj.decode('utf-8', errors='replace')}",
                                                                            every_s=1.0,
                                                                        )
                                                        except Exception:
                                                            pass
                                                    if info_n:
                                                        try:
                                                            setattr(self, "_adaos_rx_info_at", time.monotonic())
                                                        except Exception:
                                                            pass
                                                        # Parse INFO and log a couple of key fields (avoid printing nonce/connect_urls).
                                                        # This helps detect server-side max_payload / headers support issues when large PUBs
                                                        # appear to trigger immediate closes (1006/UnexpectedEOF).
                                                        try:
                                                            if head.startswith(b"INFO "):
                                                                line_end = head.find(b"\n")
                                                                if line_end < 0:
                                                                    line_end = len(head)
                                                                line = head[:line_end].strip()
                                                                if line.endswith(b"\r"):
                                                                    line = line[:-1]
                                                                js0 = line[len(b"INFO ") :].strip()
                                                                import json as _json

                                                                obj = _json.loads(js0.decode("utf-8", errors="replace"))
                                                                if isinstance(obj, dict):
                                                                    max_payload = obj.get("max_payload", None)
                                                                    version = obj.get("version", None)
                                                                    headers = obj.get("headers", None)
                                                                    tls_required = obj.get("tls_required", None)
                                                                    auth_required = obj.get("auth_required", None)
                                                                    try:
                                                                        setattr(self, "_adaos_nats_max_payload", max_payload)
                                                                    except Exception:
                                                                        pass
                                                                    if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_TRACE", "0") == "1":
                                                                        _rl_log(
                                                                            "nats.ws_info",
                                                                            f"[hub-io] nats INFO version={version} max_payload={max_payload} headers={headers} tls_required={tls_required} auth_required={auth_required}",
                                                                            every_s=3600.0,
                                                                        )
                                                        except Exception:
                                                            pass
                                            except Exception:
                                                pass
                                            return bytes(d)
                                        if isinstance(d, str):
                                            return d.encode("utf-8")
                                    except Exception:
                                        return b""

                            try:
                                setattr(_ws_readline_safe, "_adaos_ws_patch", True)
                                setattr(_ws_readline_safe, "_adaos_ws_patch_v", PATCH_V)
                            except Exception:
                                pass
                            try:
                                setattr(_ws_tr, "readline", _ws_readline_safe)
                            except Exception:
                                pass
                            patched_any = True
                            ws_safe_mode = True

                        # Trace outgoing NATS protocol keepalives without logging payloads.
                        if (
                            _ws_tr is not None
                            and callable(_orig_write)
                            and _needs_patch(_orig_write)
                        ):
                            def _ws_write_logged(self, payload):  # type: ignore[no-redef]
                                try:
                                    if os.getenv("HUB_NATS_PING_TRACE", "0") == "1" or os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                        bb = None
                                        if isinstance(payload, (bytes, bytearray, memoryview)):
                                            bb = bytes(payload)
                                        elif isinstance(payload, str):
                                            bb = payload.encode("utf-8")
                                        if isinstance(bb, (bytes, bytearray)) and len(bb) <= 16 and bb in (b"PING\r\n", b"PONG\r\n"):
                                            kind = "PING" if bb.startswith(b"PING") else "PONG"
                                            _rl_log("nats.ws_tx_nats_pp", f"[hub-io] nats ws tx data {kind}", every_s=1.0)
                                        if isinstance(bb, (bytes, bytearray)) and (os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1"):
                                            head = bytes(bb[:512])
                                            connect_n = (1 if head.startswith(b"CONNECT ") else 0) + head.count(b"\nCONNECT ") + head.count(b"\r\nCONNECT ")
                                            sub_n = (1 if head.startswith(b"SUB ") else 0) + head.count(b"\nSUB ") + head.count(b"\r\nSUB ")
                                            pub_n = (1 if head.startswith(b"PUB ") else 0) + head.count(b"\nPUB ") + head.count(b"\r\nPUB ")
                                            ping_n = head.count(b"PING\r\n")
                                            pong_n = head.count(b"PONG\r\n")
                                            err_n = 1 if b"-ERR" in head else 0
                                            if connect_n or sub_n or pub_n or ping_n or pong_n or err_n:
                                                _rl_log(
                                                    "nats.ws_tx_wiretap",
                                                    f"[hub-io] nats ws tx wiretap len={len(bb)} connect={connect_n} sub={sub_n} pub={pub_n} ping={ping_n} pong={pong_n} err={err_n}",
                                                    every_s=1.0,
                                                )
                                                if connect_n:
                                                    try:
                                                        setattr(self, "_adaos_tx_connect_at", time.monotonic())
                                                    except Exception:
                                                        pass
                                            # Optional: extract subject from PUB/SUB line (no payload).
                                            if os.getenv("HUB_NATS_TRACE_SUBJECTS", "0") == "1" or os.getenv("HUB_ROUTE_VERBOSE", "0") == "1":
                                                try:
                                                    if head.startswith(b"PUB ") or head.startswith(b"SUB "):
                                                        line_end = head.find(b"\n")
                                                        if line_end < 0:
                                                            line_end = len(head)
                                                        line = head[:line_end]
                                                        parts = line.split()
                                                        subj = parts[1] if len(parts) >= 2 else b""
                                                        if subj.startswith(b"route.to_browser.") or subj.startswith(b"route.to_hub.") or subj.startswith(b"tg."):
                                                            _rl_log(
                                                                "nats.ws_tx_subject",
                                                                f"[hub-io] nats ws tx {('PUB' if head.startswith(b'PUB ') else 'SUB')} subj={subj.decode('utf-8', errors='replace')}",
                                                                every_s=1.0,
                                                            )
                                                except Exception:
                                                    pass
                                except Exception:
                                    pass
                                return _orig_write(self, payload)

                            try:
                                setattr(_ws_write_logged, "_adaos_ws_patch", True)
                                setattr(_ws_write_logged, "_adaos_ws_patch_v", PATCH_V)
                            except Exception:
                                pass
                            try:
                                setattr(_ws_tr, "write", _ws_write_logged)
                            except Exception:
                                pass
                            patched_any = True

                        # Transport flush path uses writelines() (not write()).
                        if (
                            _ws_tr is not None
                            and callable(_orig_writelines)
                            and _needs_patch(_orig_writelines)
                        ):
                            def _ws_writelines_logged(self, payload):  # type: ignore[no-redef]
                                try:
                                    if os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                        connect_n = sub_n = pub_n = ping_n = pong_n = err_n = 0
                                        total_len = 0
                                        subj_samples: list[tuple[str, str]] = []
                                        try:
                                            it = payload if isinstance(payload, (list, tuple)) else []
                                        except Exception:
                                            it = []
                                        for msg in it:
                                            bb = None
                                            if isinstance(msg, (bytes, bytearray, memoryview)):
                                                bb = bytes(msg)
                                            elif isinstance(msg, str):
                                                bb = msg.encode("utf-8")
                                            if not isinstance(bb, (bytes, bytearray)):
                                                continue
                                            total_len += len(bb)
                                            head = bytes(bb[:512])
                                            connect_n += (1 if head.startswith(b"CONNECT ") else 0) + head.count(b"\nCONNECT ") + head.count(b"\r\nCONNECT ")
                                            sub_n += (1 if head.startswith(b"SUB ") else 0) + head.count(b"\nSUB ") + head.count(b"\r\nSUB ")
                                            pub_n += (1 if head.startswith(b"PUB ") else 0) + head.count(b"\nPUB ") + head.count(b"\r\nPUB ")
                                            ping_n += head.count(b"PING\r\n")
                                            pong_n += head.count(b"PONG\r\n")
                                            err_n += 1 if b"-ERR" in head else 0
                                            if os.getenv("HUB_NATS_TRACE_SUBJECTS", "0") == "1" or os.getenv("HUB_ROUTE_VERBOSE", "0") == "1":
                                                try:
                                                    if head.startswith(b"PUB ") or head.startswith(b"SUB "):
                                                        line_end = head.find(b"\n")
                                                        if line_end < 0:
                                                            line_end = len(head)
                                                        line = head[:line_end]
                                                        parts = line.split()
                                                        subj = parts[1] if len(parts) >= 2 else b""
                                                        if subj.startswith(b"route.to_browser.") or subj.startswith(b"route.to_hub.") or subj.startswith(b"tg."):
                                                            kind = "PUB" if head.startswith(b"PUB ") else "SUB"
                                                            subj_samples.append((kind, subj.decode("utf-8", errors="replace")))
                                                except Exception:
                                                    pass
                                        if connect_n or sub_n or pub_n or ping_n or pong_n or err_n:
                                            _rl_log(
                                                "nats.ws_tx_wiretap",
                                                f"[hub-io] nats ws tx wiretap len={total_len} connect={connect_n} sub={sub_n} pub={pub_n} ping={ping_n} pong={pong_n} err={err_n}",
                                                every_s=1.0,
                                            )
                                        if subj_samples:
                                            # Log a few samples per flush to avoid flooding.
                                            for kind, subj in subj_samples[:3]:
                                                _rl_log(
                                                    "nats.ws_tx_subject",
                                                    f"[hub-io] nats ws tx {kind} subj={subj}",
                                                    every_s=1.0,
                                                )
                                        if connect_n:
                                            try:
                                                setattr(self, "_adaos_tx_connect_at", time.monotonic())
                                            except Exception:
                                                pass
                                except Exception:
                                    pass
                                return _orig_writelines(self, payload)

                            try:
                                setattr(_ws_writelines_logged, "_adaos_ws_patch", True)
                                setattr(_ws_writelines_logged, "_adaos_ws_patch_v", PATCH_V)
                            except Exception:
                                pass
                            try:
                                setattr(_ws_tr, "writelines", _ws_writelines_logged)
                            except Exception:
                                pass
                            patched_any = True

                        # Log send failures at the actual WS write point (`drain()` -> `send_bytes()`), not only when
                        # nats-py later surfaces UnexpectedEOF. This helps distinguish "remote reset" vs "local close"
                        # and correlates failures to specific PUB/SUB operations (without logging payloads).
                        if (
                            _ws_tr is not None
                            and callable(_orig_drain)
                            and _needs_patch(_orig_drain)
                        ):
                            async def _ws_drain_logged(self):  # type: ignore[no-redef]
                                ws = getattr(self, "_ws", None)
                                if ws is None:
                                    return await _orig_drain(self)
                                while not self._pending.empty():
                                    message = self._pending.get_nowait()
                                    msg_len = None
                                    kind = None
                                    subj = None
                                    send_kind = None
                                    try:
                                        setattr(self, "_adaos_last_tx_at", time.monotonic())
                                    except Exception:
                                        pass
                                    try:
                                        msg_len = len(message) if hasattr(message, "__len__") else None
                                    except Exception:
                                        msg_len = None
                                    try:
                                        head = None
                                        if isinstance(message, (bytes, bytearray)):
                                            head = bytes(message[:256])
                                        elif isinstance(message, memoryview):
                                            head = message[:256].tobytes()
                                        if isinstance(head, (bytes, bytearray)):
                                            if head.startswith(b"PUB "):
                                                kind = "PUB"
                                            elif head.startswith(b"SUB "):
                                                kind = "SUB"
                                            elif head.startswith(b"CONNECT "):
                                                kind = "CONNECT"
                                            elif head.startswith(b"PING"):
                                                kind = "PING"
                                            elif head.startswith(b"PONG"):
                                                kind = "PONG"
                                            if kind in ("PUB", "SUB"):
                                                line_end = head.find(b"\n")
                                                if line_end < 0:
                                                    line_end = len(head)
                                                parts = head[:line_end].split()
                                                if len(parts) >= 2:
                                                    subj = parts[1].decode("utf-8", errors="replace")
                                    except Exception:
                                        kind = kind or None
                                        subj = subj or None
                                    try:
                                        setattr(self, "_adaos_last_tx_kind", kind)
                                    except Exception:
                                        pass
                                    try:
                                        setattr(self, "_adaos_last_tx_subj", subj)
                                    except Exception:
                                        pass
                                    try:
                                        setattr(self, "_adaos_last_tx_len", msg_len)
                                    except Exception:
                                        pass
                                    try:
                                        send_fn = getattr(ws, "send_bytes", None)
                                        if callable(send_fn):
                                            send_kind = "send_bytes"
                                        else:
                                            send_fn = getattr(ws, "send", None)
                                            send_kind = "send"
                                        if not callable(send_fn):
                                            raise RuntimeError("ws: no send method")
                                        await send_fn(message)
                                    except Exception as _tx_e:
                                        try:
                                            if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_TRACE", "0") == "1" or os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1":
                                                ws_closed = getattr(ws, "closed", None)
                                                ws_close_code = getattr(ws, "close_code", None)
                                                ws_close_reason = getattr(ws, "close_reason", None)
                                                ws_exc = None
                                                try:
                                                    exf = getattr(ws, "exception", None)
                                                    if callable(exf):
                                                        ws_exc = exf()
                                                except Exception:
                                                    ws_exc = None

                                                _rl_log(
                                                    "nats.ws_send_exc",
                                                    f"[hub-io] nats ws send failed fn={send_kind} err={type(_tx_e).__name__}: {_tx_e} msg_len={msg_len} kind={kind} subj={subj} tag={getattr(self, '_adaos_ws_tag', None)} ws_url={getattr(self, '_adaos_ws_url', None)} ws_closed={ws_closed} close_code={ws_close_code} close_reason={ws_close_reason} ws_exc={ws_exc}",
                                                    every_s=1.0,
                                                )
                                        except Exception:
                                            pass
                                        raise

                            try:
                                setattr(_ws_drain_logged, "_adaos_ws_patch", True)
                                setattr(_ws_drain_logged, "_adaos_ws_patch_v", PATCH_V)
                            except Exception:
                                pass
                            try:
                                setattr(_ws_tr, "drain", _ws_drain_logged)
                            except Exception:
                                pass
                            patched_any = True

                        # Log whether the hub actually receives NATS protocol PINGs (and therefore sends PONGs).
                        # This helps distinguish "connection drops because we don't respond to keepalive" from
                        # "connection drops despite healthy ping/pong".
                        if (
                            callable(_orig_process_ping)
                            and _needs_patch(_orig_process_ping)
                        ):
                            async def _process_ping_logged(self) -> None:  # type: ignore[no-redef]
                                try:
                                    if os.getenv("HUB_NATS_PING_TRACE", "0") == "1" or os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                        _rl_log("nats.rx_ping", "[hub-io] nats rx PING (will reply PONG)", every_s=1.0)
                                except Exception:
                                    pass
                                return await _orig_process_ping(self)

                            try:
                                setattr(_process_ping_logged, "_adaos_ws_patch", True)
                                setattr(_process_ping_logged, "_adaos_ws_patch_v", PATCH_V)
                            except Exception:
                                pass
                            try:
                                setattr(getattr(_nats_client, "Client", object), "_process_ping", _process_ping_logged)
                            except Exception:
                                pass
                            patched_any = True

                        # Patch connect() to add observability for WS params and attach our per-connection tag.
                        # NOTE: do NOT disable aiohttp autoping/autoclose: some proxies/servers rely on WS-level
                        # PING/PONG. Disabling autoping can cause periodic 1006 disconnects (browser sees hub_unreachable).
                        if (
                            _ws_tr is not None
                            and callable(_orig_connect)
                            and _needs_patch(_orig_connect)
                        ):
                            async def _ws_connect_safe(self, uri, buffer_size, connect_timeout):  # type: ignore[no-redef]
                                hb = None
                                try:
                                    # NOTE: aiohttp's heartbeat sends WS PINGs and expects WS PONGs; if the server/proxy
                                    # doesn't respond correctly this can cause periodic disconnects (~2*heartbeat).
                                    # Keep default OFF; enable explicitly via env.
                                    hb = _ws_heartbeat_from_env()
                                except Exception:
                                    hb = None
                                headers = self._get_custom_headers()
                                try:
                                    if isinstance(ws_connect_tag, str) and ws_connect_tag:
                                        headers = dict(headers or {})
                                        headers["X-AdaOS-Nats-Conn"] = ws_connect_tag
                                except Exception:
                                    pass
                                # aiohttp's `max_msg_size`:
                                # - unset/empty => use aiohttp default
                                # - 0 => unlimited
                                # - >0 => explicit cap
                                max_msg_size_raw = os.getenv("HUB_NATS_WS_MAX_MSG_SIZE")
                                max_msg_size_kw = None
                                try:
                                    if max_msg_size_raw is not None and str(max_msg_size_raw).strip() != "":
                                        v = int(str(max_msg_size_raw).strip())
                                        if v < 0:
                                            v = 0
                                        max_msg_size_kw = v
                                except Exception:
                                    max_msg_size_kw = None
                                try:
                                    if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_TRACE", "0") == "1":
                                            _rl_log(
                                                "nats.ws_connect",
                                                f"[hub-io] nats ws_connect uri={uri.geturl()} heartbeat={hb} ws_safe={int(ws_safe_mode)} autoping=1 max_msg_size={max_msg_size_kw if max_msg_size_kw is not None else 'default'} tag={'1' if isinstance(ws_connect_tag,str) and ws_connect_tag else '0'}",
                                                every_s=1.0,
                                            )
                                except Exception:
                                    pass
                                ws_kwargs: dict[str, Any] = {
                                    "timeout": connect_timeout,
                                    "headers": headers,
                                    "protocols": ("nats",),
                                    "autoping": True,
                                    "autoclose": True,
                                    "heartbeat": hb,
                                }
                                if max_msg_size_kw is not None:
                                    ws_kwargs["max_msg_size"] = int(max_msg_size_kw)
                                if use_ws_lib and websockets_mod is not None:
                                    try:
                                        if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_TRACE", "0") == "1":
                                            _rl_log(
                                                "nats.ws_connect_ws",
                                                f"[hub-io] nats ws_connect(websockets) uri={uri.geturl()} tag={'1' if isinstance(ws_connect_tag,str) and ws_connect_tag else '0'}",
                                                every_s=1.0,
                                            )
                                    except Exception:
                                        pass
                                    try:
                                        ws_connect_kwargs: dict[str, Any] = {
                                            "subprotocols": ["nats"],
                                            "open_timeout": connect_timeout,
                                            "ping_interval": None,
                                            "ping_timeout": None,
                                            "close_timeout": 2.0,
                                            "max_size": None,
                                            "compression": None,
                                        }
                                        # websockets changed header kw name across major versions:
                                        # - legacy: extra_headers
                                        # - modern: additional_headers
                                        try:
                                            self._ws = await websockets_mod.connect(
                                                uri.geturl(),
                                                additional_headers=_ws_additional_headers(headers),
                                                **ws_connect_kwargs,
                                            )
                                        except TypeError:
                                            self._ws = await websockets_mod.connect(
                                                uri.geturl(),
                                                extra_headers=_ws_additional_headers(headers),
                                                **ws_connect_kwargs,
                                            )
                                        self._using_tls = False
                                    except Exception as _ws_e:
                                        # If websockets is forced, do not silently fall back.
                                        if ws_impl in ("websockets", "ws"):
                                            raise
                                        try:
                                            _rl_log(
                                                "nats.ws_connect_ws_fail",
                                                f"[hub-io] nats ws_connect(websockets) failed err={type(_ws_e).__name__}: {_ws_e}; falling back to aiohttp",
                                                every_s=1.0,
                                            )
                                        except Exception:
                                            pass
                                        self._ws = await self._client.ws_connect(uri.geturl(), **ws_kwargs)
                                        self._using_tls = False
                                else:
                                    self._ws = await self._client.ws_connect(uri.geturl(), **ws_kwargs)
                                    self._using_tls = False
                                try:
                                    setattr(self, "_adaos_ws_heartbeat", hb)
                                except Exception:
                                    pass
                                try:
                                    setattr(self, "_adaos_ws_tag", ws_connect_tag)
                                except Exception:
                                    pass
                                try:
                                    setattr(self, "_adaos_ws_url", uri.geturl())
                                except Exception:
                                    pass
                                try:
                                    proto = getattr(self._ws, "protocol", None)
                                    if not proto:
                                        proto = getattr(self._ws, "subprotocol", None)
                                    setattr(self, "_adaos_ws_proto", proto)
                                except Exception:
                                    pass

                            try:
                                setattr(_ws_connect_safe, "_adaos_ws_patch", True)
                                setattr(_ws_connect_safe, "_adaos_ws_patch_v", PATCH_V)
                            except Exception:
                                pass
                            try:
                                setattr(_ws_tr, "connect", _ws_connect_safe)
                            except Exception:
                                pass
                            patched_any = True

                        # Same as above, but for `wss://` (TLS) URLs which use connect_tls().
                        if (
                            _ws_tr is not None
                            and callable(_orig_connect_tls)
                            and _needs_patch(_orig_connect_tls)
                        ):
                            async def _ws_connect_tls_safe(self, uri, ssl_context, buffer_size, connect_timeout):  # type: ignore[no-redef]
                                hb = None
                                try:
                                    hb = _ws_heartbeat_from_env()
                                except Exception:
                                    hb = None
                                # Mirror upstream behavior (refuse upgrading a live non-TLS socket).
                                try:
                                    if getattr(self, "_ws", None) is not None and not getattr(self._ws, "closed", True):
                                        if getattr(self, "_using_tls", None):
                                            return
                                        raise _NatsProtocolError("ws: cannot upgrade to TLS")
                                except Exception:
                                    # If something goes wrong introspecting state, fall back to opening a new ws.
                                    pass

                                headers = self._get_custom_headers()
                                try:
                                    if isinstance(ws_connect_tag, str) and ws_connect_tag:
                                        headers = dict(headers or {})
                                        headers["X-AdaOS-Nats-Conn"] = ws_connect_tag
                                except Exception:
                                    pass
                                target = uri if isinstance(uri, str) else uri.geturl()
                                # aiohttp's `max_msg_size`:
                                # - unset/empty => use aiohttp default
                                # - 0 => unlimited
                                # - >0 => explicit cap
                                max_msg_size_raw = os.getenv("HUB_NATS_WS_MAX_MSG_SIZE")
                                max_msg_size_kw = None
                                try:
                                    if max_msg_size_raw is not None and str(max_msg_size_raw).strip() != "":
                                        v = int(str(max_msg_size_raw).strip())
                                        if v < 0:
                                            v = 0
                                        max_msg_size_kw = v
                                except Exception:
                                    max_msg_size_kw = None
                                try:
                                    if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_TRACE", "0") == "1":
                                            _rl_log(
                                                "nats.ws_connect_tls",
                                                f"[hub-io] nats ws_connect_tls uri={target} heartbeat={hb} ws_safe={int(ws_safe_mode)} autoping=1 max_msg_size={max_msg_size_kw if max_msg_size_kw is not None else 'default'} tag={'1' if isinstance(ws_connect_tag,str) and ws_connect_tag else '0'}",
                                                every_s=1.0,
                                            )
                                except Exception:
                                    pass
                                ws_kwargs: dict[str, Any] = {
                                    "ssl": ssl_context,
                                    "timeout": connect_timeout,
                                    "headers": headers,
                                    "protocols": ("nats",),
                                    "autoping": True,
                                    "autoclose": True,
                                    "heartbeat": hb,
                                }
                                if max_msg_size_kw is not None:
                                    ws_kwargs["max_msg_size"] = int(max_msg_size_kw)
                                if use_ws_lib and websockets_mod is not None:
                                    try:
                                        if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_TRACE", "0") == "1":
                                            _rl_log(
                                                "nats.ws_connect_tls_ws",
                                                f"[hub-io] nats ws_connect_tls(websockets) uri={target} tag={'1' if isinstance(ws_connect_tag,str) and ws_connect_tag else '0'}",
                                                every_s=1.0,
                                            )
                                    except Exception:
                                        pass
                                    try:
                                        ws_connect_kwargs: dict[str, Any] = {
                                            "ssl": ssl_context,
                                            "subprotocols": ["nats"],
                                            "open_timeout": connect_timeout,
                                            "ping_interval": None,
                                            "ping_timeout": None,
                                            "close_timeout": 2.0,
                                            "max_size": None,
                                            "compression": None,
                                        }
                                        try:
                                            self._ws = await websockets_mod.connect(
                                                target,
                                                additional_headers=_ws_additional_headers(headers),
                                                **ws_connect_kwargs,
                                            )
                                        except TypeError:
                                            self._ws = await websockets_mod.connect(
                                                target,
                                                extra_headers=_ws_additional_headers(headers),
                                                **ws_connect_kwargs,
                                            )
                                        self._using_tls = True
                                    except Exception as _ws_e:
                                        if ws_impl in ("websockets", "ws"):
                                            raise
                                        try:
                                            _rl_log(
                                                "nats.ws_connect_tls_ws_fail",
                                                f"[hub-io] nats ws_connect_tls(websockets) failed err={type(_ws_e).__name__}: {_ws_e}; falling back to aiohttp",
                                                every_s=1.0,
                                            )
                                        except Exception:
                                            pass
                                        self._ws = await self._client.ws_connect(target, **ws_kwargs)
                                        self._using_tls = True
                                else:
                                    self._ws = await self._client.ws_connect(target, **ws_kwargs)
                                    self._using_tls = True
                                try:
                                    setattr(self, "_adaos_ws_heartbeat", hb)
                                except Exception:
                                    pass
                                try:
                                    setattr(self, "_adaos_ws_tag", ws_connect_tag)
                                except Exception:
                                    pass
                                try:
                                    setattr(self, "_adaos_ws_url", target)
                                except Exception:
                                    pass
                                try:
                                    proto = getattr(self._ws, "protocol", None)
                                    if not proto:
                                        proto = getattr(self._ws, "subprotocol", None)
                                    setattr(self, "_adaos_ws_proto", proto)
                                except Exception:
                                    pass

                            try:
                                setattr(_ws_connect_tls_safe, "_adaos_ws_patch", True)
                                setattr(_ws_connect_tls_safe, "_adaos_ws_patch_v", PATCH_V)
                            except Exception:
                                pass
                            try:
                                setattr(_ws_tr, "connect_tls", _ws_connect_tls_safe)
                            except Exception:
                                pass
                        patched_any = True

                        # Patch at_eof() to support websockets transport (it doesn't expose `.closed` like aiohttp).
                        if (
                            _ws_tr is not None
                            and callable(_orig_at_eof)
                            and _needs_patch(_orig_at_eof)
                        ):
                            def _ws_at_eof_safe(self):  # type: ignore[no-redef]
                                ws_obj = getattr(self, "_ws", None)
                                if ws_obj is None:
                                    try:
                                        return _orig_at_eof(self)
                                    except Exception:
                                        return True
                                closed = getattr(ws_obj, "closed", None)
                                if closed is not None:
                                    try:
                                        return bool(closed)
                                    except Exception:
                                        return True
                                st = getattr(ws_obj, "state", None)
                                try:
                                    return str(st).endswith("CLOSED")
                                except Exception:
                                    return False

                            try:
                                setattr(_ws_at_eof_safe, "_adaos_ws_patch", True)
                                setattr(_ws_at_eof_safe, "_adaos_ws_patch_v", PATCH_V)
                            except Exception:
                                pass
                            try:
                                setattr(_ws_tr, "at_eof", _ws_at_eof_safe)
                            except Exception:
                                pass
                            patched_any = True

                        # nats-py 2.12.0 can crash its _reading_task with InvalidStateError if a late PONG arrives
                        # after the future was cancelled/timed out. Make _process_pong idempotent.
                        if callable(_orig_process_pong) and _needs_patch(_orig_process_pong):
                            async def _process_pong_safe(self) -> None:  # type: ignore[no-redef]
                                # nats-py can leave cancelled/done futures in `self._pongs` (e.g. flush timeout),
                                # and later crash when a PONG arrives and it tries to set_result().
                                #
                                # IMPORTANT: regardless of future state, a PONG means the connection is alive and
                                # `_pings_outstanding` must be reset; otherwise ping-interval can falsely trigger
                                # ErrStaleConnection and flap the WS tunnel.
                                try:
                                    pongs = getattr(self, "_pongs", None)
                                    if isinstance(pongs, list):
                                        # Drop cancelled/done futures first.
                                        while pongs and getattr(pongs[0], "done", lambda: False)():
                                            try:
                                                pongs.pop(0)
                                            except Exception:
                                                break
                                        if pongs:
                                            future = pongs.pop(0)
                                            try:
                                                if not future.cancelled() and not future.done():
                                                    future.set_result(True)
                                            except asyncio.InvalidStateError:
                                                pass
                                    try:
                                        self._pongs_received += 1
                                    except Exception:
                                        pass
                                finally:
                                    try:
                                        self._pings_outstanding = 0
                                    except Exception:
                                        pass
                                try:
                                    if (
                                        os.getenv("HUB_NATS_PING_TRACE", "0") == "1"
                                        or os.getenv("HUB_NATS_TRACE_INPUT", "0") == "1"
                                        or os.getenv("HUB_NATS_VERBOSE", "0") == "1"
                                    ):
                                        _rl_log("nats.rx_pong", "[hub-io] nats rx PONG", every_s=1.0)
                                except Exception:
                                    pass

                            try:
                                setattr(_process_pong_safe, "_adaos_ws_patch", True)
                                setattr(_process_pong_safe, "_adaos_ws_patch_v", PATCH_V)
                            except Exception:
                                pass
                            try:
                                setattr(_nats_client.Client, "_process_pong", _process_pong_safe)
                            except Exception:
                                pass
                            patched_any = True

                        if patched_any:
                            try:
                                import importlib.metadata as _md  # type: ignore

                                _nats_ver = None
                                try:
                                    _nats_ver = _md.version("nats-py")
                                except Exception:
                                    _nats_ver = None
                                _rl_log(
                                    "nats.patch",
                                    f"[hub-io] nats ws patch applied (nats-py={_nats_ver} aiohttp={getattr(aiohttp,'__version__',None)})",
                                    every_s=3600.0,
                                )
                            except Exception:
                                pass
                        else:
                            try:
                                if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_TRACE", "0") == "1":
                                    _rl_log(
                                        "nats.patch_none",
                                        f"[hub-io] nats ws patch not applied in this boot (hooks may already be patched) (ws_tr={type(_ws_tr).__name__ if _ws_tr is not None else None} rl={callable(_orig_rl)} write={callable(_orig_write)} writelines={callable(_orig_writelines)} connect={callable(_orig_connect)} connect_tls={callable(_orig_connect_tls)})",
                                        every_s=3600.0,
                                    )
                            except Exception:
                                pass
                        # Always print a compact patch status summary in verbose/trace mode so we can verify which
                        # hooks are active when diagnosing 1006/UnexpectedEOF flaps.
                        try:
                            if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_TRACE", "0") == "1":
                                def _has_patch(fn: Any | None) -> bool:
                                    try:
                                        return bool(getattr(fn, "_adaos_ws_patch", False))
                                    except Exception:
                                        return False

                                st_rl = _has_patch(getattr(_ws_tr, "readline", None)) if _ws_tr else False
                                st_wr = _has_patch(getattr(_ws_tr, "write", None)) if _ws_tr else False
                                st_wrl = _has_patch(getattr(_ws_tr, "writelines", None)) if _ws_tr else False
                                st_c = _has_patch(getattr(_ws_tr, "connect", None)) if _ws_tr else False
                                st_ct = _has_patch(getattr(_ws_tr, "connect_tls", None)) if _ws_tr else False
                                st_ping = _has_patch(getattr(getattr(_nats_client, "Client", object), "_process_ping", None))
                                st_pong = _has_patch(getattr(getattr(_nats_client, "Client", object), "_process_pong", None))
                                _rl_log(
                                    "nats.patch_status",
                                    f"[hub-io] nats ws patch status: rl={int(st_rl)} wr={int(st_wr)} wrl={int(st_wrl)} c={int(st_c)} ct={int(st_ct)} ping={int(st_ping)} pong={int(st_pong)}",
                                    every_s=1.0,
                                )
                        except Exception:
                            pass
                    except Exception as _patch_e:
                        try:
                            if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_TRACE", "0") == "1":
                                _rl_log(
                                    "nats.patch_exc",
                                    f"[hub-io] nats ws patch error: {type(_patch_e).__name__}: {_patch_e}",
                                    every_s=1.0,
                                )
                        except Exception:
                            pass

                    def _explain_connect_error(err: Exception) -> str:
                        try:
                            msg = str(err) or ""
                            low = msg.lower()
                            if isinstance(err, TypeError) and "argument of type 'int' is not iterable" in low:
                                return "root nats authentication error: WS closed after CONNECT; " "verify node.yaml nats.user=hub_<subnet_id> and nats.pass=<hub_nats_token>"
                        except Exception:
                            pass
                        # fallback – include class and message
                        try:
                            return f"{type(err).__name__}: {str(err)}"
                        except Exception:
                            return type(err).__name__

                    def _looks_like_auth_failure(err: Exception) -> bool:
                        """
                        Heuristic: when root-side NATS WS proxy closes after CONNECT because of invalid credentials,
                        nats-py can surface confusing exceptions (historically observed in this project).
                        Treat these as auth-ish failures and trigger credential refresh.
                        """
                        try:
                            msg = str(err) or ""
                            low = msg.lower()
                            if isinstance(err, TypeError) and "argument of type 'int' is not iterable" in low:
                                return True
                            if "authentication timeout" in low:
                                return True
                            if "authorization violation" in low:
                                return True
                            if "auth" in low:
                                return True
                            if type(err).__name__ == "UnexpectedEOF" or "unexpected eof" in low:
                                return True
                        except Exception:
                            return False
                        return False

                    while True:
                        try:
                            nurl, nuser, npass = _read_node_nats()
                            if not nurl or not nuser or not npass:
                                fetched = await _fetch_nats_credentials()
                                if fetched:
                                    # re-read node.yaml on next loop
                                    await asyncio.sleep(0.1)
                                    continue
                                # Wait for `adaos dev telegram` to provision credentials.
                                if os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                    print("[hub-io] NATS disabled: missing nats.ws_url/user/pass in node.yaml")
                                await asyncio.sleep(2.0)
                                continue

                            user = nuser
                            pw = npass
                            pw_mask = (pw[:3] + "***" + pw[-2:]) if pw and len(pw) > 6 else ("***" if pw else None)
                            # Build candidates without mixing WS and TCP schemes to avoid client errors.
                            candidates: List[str] = []

                            def _dedup_push(url: str) -> None:
                                if not url:
                                    return
                                s = str(url).strip()
                                if not s:
                                    return
                                # For NATS WS clients, it's safer to always have an explicit WS path.
                                # In our deployment NATS WS is mounted at `/nats` (not `/`).
                                if s.startswith("ws://") or s.startswith("wss://"):
                                    ws_default_path = os.getenv("NATS_WS_DEFAULT_PATH", "/nats") or "/nats"
                                    if not ws_default_path.startswith("/"):
                                        ws_default_path = "/" + ws_default_path
                                    try:
                                        from urllib.parse import urlparse, urlunparse

                                        pr0 = urlparse(s)
                                        # Keep an explicit "/" WS mount intact: some deployments terminate WS on "/".
                                        # Only inject the default mount when the path is missing entirely.
                                        if not pr0.path:
                                            pr0 = pr0._replace(path=ws_default_path)
                                            s = urlunparse(pr0)
                                    except Exception:
                                        if s.endswith("://") or s.endswith("://localhost") or s.endswith("://127.0.0.1"):
                                            s = s.rstrip("/") + ws_default_path
                                if s not in candidates:
                                    candidates.append(s)

                            base = (nurl or "").rstrip("/")

                            try:
                                from urllib.parse import urlparse, urlunparse

                                pr = urlparse(base) if base else None
                                scheme = (pr.scheme if pr else "").lower()
                                # If base is http(s), normalize to ws(s)
                                if scheme in ("http", "https"):
                                    base = "ws" + base[4:]
                                    pr = urlparse(base)
                                    scheme = pr.scheme.lower()
                                # Default to WS mode when uncertain or when base points to cluster alias
                                is_ws_mode = (not base) or scheme.startswith("ws")
                                if not is_ws_mode and scheme == "nats":
                                    host = (pr.hostname or "").lower()
                                    # Avoid using internal docker alias from host-based hub
                                    if host in ("nats", "localhost", "127.0.0.1"):
                                        is_ws_mode = True

                                if is_ws_mode:
                                    # Prefer WS endpoints only.
                                    # IMPORTANT: Keep this conservative — probing extra mounts/hosts has caused
                                    # "Authentication Timeout" hangs when we accidentally hit non-NATS WS endpoints.
                                    if base:
                                        _dedup_push(base)
                                    # Known public endpoint as a fallback (explicitly WS-nats proxy).
                                    _dedup_push("wss://nats.inimatic.com/nats")
                                    _dedup_push("wss://api.inimatic.com/nats")
                                    # Allow explicit WS alternates via env (comma-separated)
                                    extra = os.getenv("NATS_WS_URL_ALT")
                                    if extra:
                                        for it in [x.strip() for x in extra.split(",") if x.strip()]:
                                            if it.startswith("ws"):
                                                _dedup_push(it)
                                else:
                                    # TCP mode: only nats:// endpoints
                                    if base:
                                        _dedup_push(base)
                                    # Optional TCP alternates via env (comma-separated)
                                    extra = os.getenv("NATS_TCP_URL_ALT")
                                    if extra:
                                        for it in [x.strip() for x in extra.split(",") if x.strip()]:
                                            if it.startswith("nats://"):
                                                _dedup_push(it)
                            except Exception:
                                # Fallback: if base present, use it only; otherwise default to WS domain
                                if base:
                                    _dedup_push(base)
                                else:
                                    _dedup_push("wss://nats.inimatic.com")

                            # Keep both `/nats` WS entrypoints:
                            # - `wss://api.inimatic.com/nats` (root ingress)
                            # - `wss://nats.inimatic.com/nats` (dedicated hostname)
                            # Some environments historically observed HTTP 400 on the api-domain upgrade, but
                            # keeping it as a candidate is safer than hard-filtering it out (it can be the only
                            # reachable WS endpoint on certain networks).
                            try:
                                now_m = time.monotonic()
                                available = [s for s in candidates if now_m >= float(nats_server_quarantine_until.get(str(s), 0.0))]
                                if available:
                                    candidates = available
                            except Exception:
                                pass

                            # Prefer the dedicated hostname over the root ingress when both are available.
                            try:
                                pref_ded = os.getenv("HUB_NATS_PREFER_DEDICATED", "1")
                                preferred = None
                                if str(pref_ded).strip() == "1":
                                    preferred = "wss://nats.inimatic.com/nats"
                                elif str(pref_ded).strip() == "0":
                                    preferred = "wss://api.inimatic.com/nats"
                                if preferred in candidates and candidates and candidates[0] != preferred:
                                    candidates = [preferred] + [c for c in candidates if c != preferred]
                            except Exception:
                                pass

                            hub_nats_verbose = os.getenv("HUB_NATS_VERBOSE", "0") == "1"
                            hub_nats_quiet = os.getenv("HUB_NATS_QUIET", "1") == "1"
                            if hub_nats_verbose or not hub_nats_quiet:
                                print(f"[hub-io] Connecting NATS candidates={candidates} user={user} pass={pw_mask}")

                            def _emit_down(kind: str, err: Exception | None) -> None:
                                nonlocal reported_down
                                if not reported_down:
                                    et = type(err).__name__ if err else kind
                                    # Produce a richer one-time diagnostics line to aid debugging WS/TLS/DNS issues
                                    if hub_nats_verbose or not hub_nats_quiet:
                                        try:
                                            if os.getenv("SILENCE_NATS_EOF", "0") == "1" and kind == "disconnected":
                                                # Suppress idle disconnect chatter in dev
                                                pass
                                            else:
                                                details = ""
                                                if err is not None:
                                                    msg = str(err) or repr(err)
                                                    # Extract aiohttp handshake info if present
                                                    status = getattr(err, "status", None)
                                                    url = getattr(err, "url", None) or getattr(getattr(err, "request_info", None), "real_url", None)
                                                    if status:
                                                        details += f" status={status}"
                                                    if url:
                                                        details += f" url={url}"
                                                    # Include a short class:message tail
                                                    details = (details + f" msg={msg}").strip()
                                            if not (os.getenv("SILENCE_NATS_EOF", "0") == "1" and kind == "disconnected"):
                                                print(f"[hub-io] nats server unreachable ({et}){(': ' + details) if details else ''}")
                                        except Exception:
                                            pass
                                    try:
                                        self.ctx.bus.publish(
                                            Event(type="subnet.nats.down", payload={"kind": kind, "error": str(err) if err else None, "ts": time.time()}, source="io.nats")
                                        )
                                    except Exception:
                                        pass
                                    reported_down = True

                            def _emit_up() -> None:
                                nonlocal reported_down
                                if reported_down:
                                    if hub_nats_verbose or not hub_nats_quiet:
                                        try:
                                            print("[hub-io] nats connection restored")
                                        except Exception:
                                            pass
                                    try:
                                        self.ctx.bus.publish(Event(type="subnet.nats.up", payload={"ts": time.time()}, source="io.nats"))
                                    except Exception:
                                        pass
                                    reported_down = False

                            def _ws_state(nc_for_diag: Any) -> tuple[Any, Any, Any, Any]:
                                try:
                                    tr = getattr(nc_for_diag, "_transport", None)
                                    ws = getattr(tr, "_ws", None) if tr is not None else None
                                    ws_closed = getattr(ws, "closed", None) if ws is not None else None
                                    ws_close_code = getattr(ws, "close_code", None) if ws is not None else None
                                    ws_close_reason = getattr(ws, "close_reason", None) if ws is not None else None
                                    ws_exc = None
                                    try:
                                        exf = getattr(ws, "exception", None)
                                        if callable(exf):
                                            ws_exc = exf()
                                    except Exception:
                                        ws_exc = None
                                    return ws_closed, ws_close_code, ws_close_reason, ws_exc
                                except Exception:
                                    return None, None, None, None

                            def _env_is_sensitive(name: str) -> bool:
                                try:
                                    n = (name or "").upper()
                                except Exception:
                                    return False
                                return any(x in n for x in ("PASS", "PASSWORD", "TOKEN", "SECRET", "KEY", "JWT", "AUTH"))

                            def _env_snapshot(keys: list[str]) -> str:
                                parts: list[str] = []
                                for k in keys:
                                    try:
                                        v = os.getenv(k)
                                    except Exception:
                                        v = None
                                    if v is None:
                                        parts.append(f"{k}=<unset>")
                                        continue
                                    vv = str(v)
                                    if _env_is_sensitive(k):
                                        if not vv:
                                            parts.append(f"{k}=<empty>")
                                        else:
                                            parts.append(f"{k}=<set:{len(vv)}>")
                                    else:
                                        # Avoid huge env values in logs.
                                        if len(vv) > 200:
                                            vv = vv[:200] + "…"
                                        parts.append(f"{k}={vv}")
                                return " ".join(parts)

                            async def _on_error_cb(e: Exception, *, nc_for_diag: Any | None = None) -> None:
                                # Best-effort; keep quiet unless explicitly verbose or useful
                                is_eof = type(e).__name__ == "UnexpectedEOF" or "unexpected eof" in str(e).lower()
                                if os.getenv("SILENCE_NATS_EOF", "0") == "1" and is_eof:
                                    return
                                # Emit extra transport diagnostics to correlate client-side errors with root-side logs.
                                if nc_for_diag is not None and (is_eof or os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_TRACE", "0") == "1"):
                                    try:
                                        ws_closed, ws_close_code, ws_close_reason, ws_exc = _ws_state(nc_for_diag)
                                        try:
                                            tr = getattr(nc_for_diag, "_transport", None)
                                            ws = getattr(tr, "_ws", None) if tr is not None else None
                                            last_rx_at = getattr(tr, "_adaos_last_rx_at", None)
                                            last_rx_ago_s = None
                                            try:
                                                if isinstance(last_rx_at, (int, float)):
                                                    last_rx_ago_s = round(time.monotonic() - float(last_rx_at), 3)
                                            except Exception:
                                                last_rx_ago_s = None
                                            last_tx_ago_s = None
                                            try:
                                                last_tx_at = getattr(tr, "_adaos_last_tx_at", None)
                                                if isinstance(last_tx_at, (int, float)):
                                                    last_tx_ago_s = round(time.monotonic() - float(last_tx_at), 3)
                                            except Exception:
                                                last_tx_ago_s = None
                                            tx_connect_ago_s = None
                                            try:
                                                tx_connect_at = getattr(tr, "_adaos_tx_connect_at", None) if tr is not None else None
                                                if isinstance(tx_connect_at, (int, float)):
                                                    tx_connect_ago_s = round(time.monotonic() - float(tx_connect_at), 3)
                                            except Exception:
                                                tx_connect_ago_s = None
                                            rx_info_ago_s = None
                                            try:
                                                rx_info_at = getattr(tr, "_adaos_rx_info_at", None) if tr is not None else None
                                                if isinstance(rx_info_at, (int, float)):
                                                    rx_info_ago_s = round(time.monotonic() - float(rx_info_at), 3)
                                            except Exception:
                                                rx_info_ago_s = None
                                            max_payload = None
                                            try:
                                                max_payload = getattr(tr, "_adaos_nats_max_payload", None) if tr is not None else None
                                            except Exception:
                                                max_payload = None
                                            pending_data_size = getattr(nc_for_diag, "_pending_data_size", None)
                                            pings_outstanding = getattr(nc_for_diag, "_pings_outstanding", None)
                                            pongs_q = None
                                            try:
                                                pongs = getattr(nc_for_diag, "_pongs", None)
                                                if isinstance(pongs, list):
                                                    pongs_q = len(pongs)
                                            except Exception:
                                                pongs_q = None
                                            tr_pending_q = None
                                            try:
                                                q = getattr(tr, "_pending", None) if tr is not None else None
                                                if q is not None:
                                                    tr_pending_q = q.qsize()
                                            except Exception:
                                                tr_pending_q = None
                                            ws_tag = None
                                            try:
                                                ws_tag = getattr(tr, "_adaos_ws_tag", None) if tr is not None else None
                                            except Exception:
                                                ws_tag = None
                                            if not ws_tag:
                                                try:
                                                    ws_tag = ws_connect_tag if isinstance(ws_connect_tag, str) else None
                                                except Exception:
                                                    ws_tag = None
                                            ws_hb = None
                                            try:
                                                ws_hb = getattr(tr, "_adaos_ws_heartbeat", None) if tr is not None else None
                                            except Exception:
                                                ws_hb = None
                                            ws_url = None
                                            try:
                                                ws_url = getattr(tr, "_adaos_ws_url", None) if tr is not None else None
                                            except Exception:
                                                ws_url = None
                                            ws_proto = None
                                            try:
                                                ws_proto = getattr(tr, "_adaos_ws_proto", None) if tr is not None else None
                                            except Exception:
                                                ws_proto = None
                                            if not ws_proto:
                                                try:
                                                    ws_proto = getattr(ws, "protocol", None) if ws is not None else None
                                                except Exception:
                                                    ws_proto = None
                                            if not ws_proto:
                                                try:
                                                    ws_proto = getattr(ws, "_response", None).headers.get("Sec-WebSocket-Protocol") if ws is not None and getattr(ws, "_response", None) is not None else None
                                                except Exception:
                                                    ws_proto = None
                                            last_tx_kind = None
                                            last_tx_subj = None
                                            last_tx_len = None
                                            try:
                                                last_tx_kind = getattr(tr, "_adaos_last_tx_kind", None) if tr is not None else None
                                                last_tx_subj = getattr(tr, "_adaos_last_tx_subj", None) if tr is not None else None
                                                last_tx_len = getattr(tr, "_adaos_last_tx_len", None) if tr is not None else None
                                            except Exception:
                                                last_tx_kind = last_tx_kind or None
                                                last_tx_subj = last_tx_subj or None
                                                last_tx_len = last_tx_len or None
                                            _rl_log(
                                                "nats.ws_diag",
                                                f"[hub-io] nats ws diag: tag={ws_tag} server={nats_last_server} ws_hb_s={ws_hb} ws_url={ws_url} closed={ws_closed} close_code={ws_close_code} close_reason={ws_close_reason} ws_exc={ws_exc} last_rx_ago_s={last_rx_ago_s} last_tx_ago_s={last_tx_ago_s} tx_connect_ago_s={tx_connect_ago_s} rx_info_ago_s={rx_info_ago_s} max_payload={max_payload} pending_data_size={pending_data_size} pings_outstanding={pings_outstanding} pongs_q={pongs_q} transport_pending_q={tr_pending_q} ws_proto={ws_proto} last_tx_kind={last_tx_kind} last_tx_subj={last_tx_subj} last_tx_len={last_tx_len}",
                                                every_s=1.0,
                                            )
                                        except Exception:
                                            pass
                                        _rl_log(
                                            "nats.ws_eof",
                                            f"[hub-io] nats ws eof: closed={ws_closed} close_code={ws_close_code} close_reason={ws_close_reason} ws_exc={ws_exc}",
                                            every_s=1.0,
                                        )
                                    except Exception:
                                        pass
                                # Capture the effective env knobs around NATS-over-WS on errors to make log sharing actionable.
                                try:
                                    _env = _env_snapshot(
                                        [
                                            "HUB_NATS_PING_INTERVAL_S",
                                            "HUB_NATS_MAX_OUTSTANDING_PINGS",
                                            "HUB_NATS_DISABLE_PING_INTERVAL_TASK",
                                            "HUB_NATS_RX_TIMEOUT_S",
                                            "HUB_NATS_WS_IMPL",
                                            "HUB_NATS_WS_HEARTBEAT_S",
                                            "HUB_NATS_WS_HEARTBEAT_FORCE",
                                            "HUB_NATS_WS_MAX_MSG_SIZE",
                                            "HUB_NATS_RAW_KEEPALIVE",
                                            "HUB_NATS_RAW_KEEPALIVE_S",
                                            "HUB_NATS_CONNECT_TAG_QUERY",
                                            "WS_NATS_PROXY_WS_PING",
                                            "WS_NATS_PROXY_TERMINATE_CLIENT_PING",
                                            "WS_NATS_PROXY_KEEPALIVE_REQUIRE_HANDSHAKE",
                                            "WS_NATS_PROXY_WIRETAP",
                                        ]
                                    )
                                    if _env:
                                        _rl_log("nats.env", f"[hub-io] nats env: {_env}", every_s=30.0)
                                except Exception:
                                    pass
                                try:
                                    verbose = os.getenv("HUB_NATS_VERBOSE", "0") == "1"
                                    quiet = os.getenv("HUB_NATS_QUIET", "1") == "1"
                                    if quiet and not verbose and not is_eof:
                                        return
                                    if type(e).__name__ == "WSServerHandshakeError" and not verbose:
                                        print("[hub-io] nats error_cb: WSServerHandshakeError (check nats.ws_url path: '/' vs '/nats')")
                                        return
                                    if verbose:
                                        print(f"[hub-io] nats error_cb: {type(e).__name__}: {e!s}")
                                    else:
                                        print(f"[hub-io] nats error_cb: {type(e).__name__}")
                                except Exception:
                                    pass

                            async def _on_disconnected() -> None:
                                _emit_down("disconnected", None)

                            async def _on_reconnected() -> None:
                                # Suppress restored chatter in dev if silenced
                                if os.getenv("SILENCE_NATS_EOF", "0") == "1":
                                    try:
                                        self.ctx.bus.publish(Event(type="subnet.nats.up", payload={"ts": time.time()}, source="io.nats"))
                                    except Exception:
                                        pass
                                else:
                                    _emit_up()

                            # Coerce types to what nats-py expects
                            # For WS proxy auth, always identify as the canonical hub id regardless of alias recorded in node.yaml
                            try:
                                is_ws_candidates = any(isinstance(s, str) and s.startswith("ws") for s in candidates)
                            except Exception:
                                is_ws_candidates = False
                            if is_ws_candidates:
                                # Always use canonical hub identifier for WS auth: "hub_<hub_id>"
                                user = f"hub_{hub_id}"
                            hub_id_str = hub_id if isinstance(hub_id, str) else str(hub_id)
                            user_str = user if (user is None or isinstance(user, str)) else str(user)
                            pw_str = pw if (pw is None or isinstance(pw, str)) else str(pw)
                            if os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                try:
                                    print(f"[hub-io] nats connect opts: name=hub-{hub_id_str!s} user={type(user_str).__name__} pass={type(pw_str).__name__} servers={candidates}")
                                except Exception:
                                    pass

                            # NOTE: Connect to candidates sequentially. Some endpoints can hang the WS handshake
                            # (leading to "Authentication Timeout") while others work; trying one-by-one keeps
                            # failures isolated and helps cleanup transports.
                            async def _try_connect(server: str) -> Any:
                                # `nats` package does not expose Client at top-level; use nats.aio.client.Client.
                                nc_local = _nats.aio.client.Client()
                                async def _on_error_cb_local(e: Exception) -> None:
                                    await _on_error_cb(e, nc_for_diag=nc_local)
                                try:
                                    # New correlation id for this connect attempt (sent as WS header).
                                    try:
                                        nonlocal ws_connect_tag
                                        ws_connect_tag = f"{hub_id_str}-{uuid.uuid4().hex[:10]}"
                                    except Exception:
                                        ws_connect_tag = None
                                    connect_server = str(server)
                                    # Some transports do not reliably propagate custom WS headers.
                                    # Optionally attach the correlation id as a query param to help root-side
                                    # logs correlate abnormal closes (1006/EOF) to hub attempts.
                                    try:
                                        if os.getenv("HUB_NATS_CONNECT_TAG_QUERY", "0") == "1" and isinstance(ws_connect_tag, str) and ws_connect_tag:
                                            from urllib.parse import urlparse as _urlparse, urlunparse as _urlunparse, parse_qsl as _parse_qsl, urlencode as _urlencode
                                            u = _urlparse(connect_server)
                                            q = dict(_parse_qsl(u.query, keep_blank_values=True))
                                            q.setdefault("adaos_conn", ws_connect_tag)
                                            connect_server = _urlunparse(u._replace(query=_urlencode(q)))
                                    except Exception:
                                        connect_server = str(server)
                                    try:
                                        if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                            _rl_log(
                                                "nats.connect_try",
                                                f"[hub-io] NATS connect try server={connect_server} tag={ws_connect_tag}",
                                                every_s=1.0,
                                            )
                                    except Exception:
                                        pass
                                    # Keepalive:
                                    # - Root's ws-nats-proxy sends NATS `PING\r\n` frames to the hub, but those
                                    #   only keep the WS tunnel alive if the hub actually replies with `PONG\r\n`.
                                    # - Some reverse proxies / LBs will still cut long-lived WS connections if the
                                    #   client stays silent (observed as ~1000s / close 1006 + ECONNRESET on root).
                                    #
                                    # Therefore, for WS transports default to a small hub->root ping interval to
                                    # guarantee outbound traffic even when the hub is otherwise idle.
                                    # NOTE: Some NATS-over-WS proxies (observed on inimatic ws-nats-proxy) can
                                    # flap with close 1006/UnexpectedEOF when the client sends periodic NATS PINGs.
                                    # Root/proxy already sends server PINGs, so the hub still generates outbound
                                    # traffic by replying with PONGs even if the client ping interval is conservative.
                                    try:
                                        ping_interval_default = "3600" if connect_server.startswith("ws") else "3600"
                                        ping_interval = int(
                                            os.getenv("HUB_NATS_PING_INTERVAL_S", ping_interval_default)
                                            or ping_interval_default
                                        )
                                        # nats-py always starts the ping task; 0 would create a busy-loop.
                                        if ping_interval <= 0:
                                            ping_interval = int(ping_interval_default)
                                    except Exception:
                                        ping_interval = 3600
                                    try:
                                        max_outstanding_pings = int(os.getenv("HUB_NATS_MAX_OUTSTANDING_PINGS", "10") or "10")
                                    except Exception:
                                        max_outstanding_pings = 10
                                    await asyncio.wait_for(
                                        nc_local.connect(
                                            servers=[connect_server],
                                            user=user_str,
                                            password=pw_str,
                                            name=f"hub-{hub_id_str}",
                                            allow_reconnect=False,
                                            # Be tolerant to intermittent WS proxy hiccups: missed PONGs should not
                                            # tear down the whole hub IO bridge too aggressively.
                                            ping_interval=ping_interval,
                                            max_outstanding_pings=max_outstanding_pings,
                                            connect_timeout=5.0,
                                            error_cb=_on_error_cb_local,
                                            disconnected_cb=_on_disconnected,
                                            reconnected_cb=_on_reconnected,
                                        ),
                                        timeout=7.0,
                                    )
                                    try:
                                        if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                            tr = getattr(nc_local, "_transport", None)
                                            hb = getattr(tr, "_adaos_ws_heartbeat", None) if tr else None
                                            if hb is not None:
                                                _rl_log("nats.ws_hb", f"[hub-io] nats ws heartbeat: {hb!s}s", every_s=60.0)
                                            if isinstance(ws_connect_tag, str) and ws_connect_tag:
                                                _rl_log("nats.ws_tag", f"[hub-io] nats ws tag: {ws_connect_tag}", every_s=1.0)
                                    except Exception:
                                        pass
                                    # Optionally disable periodic client PINGs on WS transports.
                                    # Some proxies respond poorly to client-initiated PINGs and can force-close (1006/EOF).
                                    # Default: disable for WS; can be re-enabled with HUB_NATS_DISABLE_PING_INTERVAL_TASK=0.
                                    try:
                                        if connect_server.startswith("ws"):
                                            disable_env = os.getenv("HUB_NATS_DISABLE_PING_INTERVAL_TASK", "1")
                                            disable_ping_task = str(disable_env or "").strip() != "0"
                                            if disable_ping_task:
                                                pt = getattr(nc_local, "_ping_interval_task", None)
                                                if isinstance(pt, asyncio.Task):
                                                    try:
                                                        if not pt.done():
                                                            pt.cancel()
                                                    except Exception:
                                                        pass
                                                    # Important: our own bridge watchdog treats core task termination as fatal.
                                                    # When we intentionally disable the ping task, clear the reference so the
                                                    # watchdog doesn't restart the whole bridge on a cancelled task.
                                                    try:
                                                        setattr(nc_local, "_ping_interval_task", None)
                                                    except Exception:
                                                        pass
                                                    try:
                                                        setattr(nc_local, "_adaos_ping_interval_task_disabled", True)
                                                    except Exception:
                                                        pass
                                                    if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                                        _rl_log(
                                                            "nats.ping_task_off",
                                                            "[hub-io] nats ping interval task disabled for WS transport",
                                                            every_s=60.0,
                                                        )
                                    except Exception:
                                        pass

                                    # Guard against nats-py flush timeout bug: flush() cancels the Future
                                    # but keeps it in `self._pongs`, causing InvalidStateError on next PONG.
                                    # Also covers shutdown races with late PONGs.
                                    try:
                                        if os.getenv("HUB_NATS_PATCH_INVALIDSTATE", "1") == "1":
                                            async def _safe_process_pong() -> None:  # type: ignore[no-redef]
                                                try:
                                                    pongs = getattr(nc_local, "_pongs", None)
                                                    if isinstance(pongs, list):
                                                        # Drop cancelled/done futures first.
                                                        while pongs and getattr(pongs[0], "done", lambda: False)():
                                                            try:
                                                                pongs.pop(0)
                                                            except Exception:
                                                                break
                                                        if pongs:
                                                            fut = pongs.pop(0)
                                                            try:
                                                                if not fut.cancelled() and not fut.done():
                                                                    fut.set_result(True)
                                                            except asyncio.InvalidStateError:
                                                                pass
                                                    # Keep bookkeeping consistent with upstream implementation.
                                                    try:
                                                        setattr(
                                                            nc_local,
                                                            "_pongs_received",
                                                            int(getattr(nc_local, "_pongs_received", 0)) + 1,
                                                        )
                                                    except Exception:
                                                        pass
                                                    try:
                                                        setattr(nc_local, "_pings_outstanding", 0)
                                                    except Exception:
                                                        pass
                                                except Exception:
                                                    return

                                            setattr(nc_local, "_process_pong", _safe_process_pong)
                                    except Exception:
                                        pass
                                    return nc_local
                                except Exception as e:
                                    # Extra diagnostics for flaky WS/NATS drops (e.g. UnexpectedEOF without close frame).
                                    try:
                                        if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                            tr = getattr(nc_local, "_transport", None)
                                            ws = getattr(tr, "_ws", None) if tr else None
                                            ws_closed = getattr(ws, "closed", None) if ws is not None else None
                                            ws_close_code = getattr(ws, "close_code", None) if ws is not None else None
                                            ws_exc = None
                                            try:
                                                exf = getattr(ws, "exception", None)
                                                if callable(exf):
                                                    ws_exc = exf()
                                            except Exception:
                                                ws_exc = None
                                            _rl_log(
                                                "nats.ws_diag",
                                                f"[hub-io] nats ws diag: tag={ws_connect_tag} server={locals().get('connect_server', None)} err={type(e).__name__} closed={ws_closed} close_code={ws_close_code} ws_exc={ws_exc}",
                                                every_s=2.0,
                                            )
                                    except Exception:
                                        pass

                                    # Best-effort token refresh on auth-ish failures.
                                    try:
                                        if _looks_like_auth_failure(e):
                                            if os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                                try:
                                                    print(
                                                        f"[hub-io] NATS auth failure suspected; refreshing credentials (err={type(e).__name__}: {e})"
                                                    )
                                                except Exception:
                                                    pass
                                            await _fetch_nats_credentials()
                                    except Exception:
                                        pass
                                    # Best-effort cleanup of partially created WS transport
                                    try:
                                        await nc_local.close()
                                    except Exception:
                                        pass
                                    # Ensure WS transport is fully torn down if connect() was cancelled/timed out.
                                    try:
                                        tr = getattr(nc_local, "_transport", None)
                                        if tr:
                                            ws = getattr(tr, "_ws", None)
                                            client = getattr(tr, "_client", None)
                                            try:
                                                if ws is not None:
                                                    await ws.close()
                                            except Exception:
                                                pass
                                            try:
                                                if client is not None:
                                                    await client.close()
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                                    raise e

                            last_exc: Exception | None = None
                            nc = None
                            connected_server: str | None = None
                            for srv in [str(s) for s in candidates]:
                                try:
                                    if os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                        print(f"[hub-io] NATS connect try server={srv}")
                                    elif trace:
                                        _rl_log("nats.try", f"[hub-io] nats connect try server={srv}", every_s=1.0)
                                    nc = await _try_connect(srv)
                                    last_exc = None
                                    connected_server = srv
                                    break
                                except Exception as e:
                                    last_exc = e
                                    if trace:
                                        _rl_log("nats.try_fail", f"[hub-io] nats connect failed server={srv} err={type(e).__name__}", every_s=1.0)
                                    continue
                            if nc is None:
                                raise last_exc or RuntimeError("nats connect failed (no candidates)")
                            try:
                                nats_last_server = connected_server
                            except Exception:
                                pass

                            # Keepalive: periodically send a tiny NATS protocol frame from hub->root.
                            #
                            # Root's WS proxy already sends NATS `PING` frames to the hub, but the main purpose of that
                            # is to elicit outbound traffic hub->root (`PONG`) to keep some NAT/firewall mappings alive.
                            # In practice, hubs sometimes end up mostly silent and the WS gets closed abnormally (1006),
                            # then hub sees `UnexpectedEOF`. To reduce dependency on nats-py's internal ping futures and
                            # ensure regular outbound traffic, optionally send raw `PING` via `_send_command`+`_flush_pending`.
                            #
                            # This avoids using `flush()` and avoids creating `_pongs` futures which can later explode
                            # with `InvalidStateError` on late/cancelled PONGs.
                            try:
                                raw_keepalive_env = os.getenv("HUB_NATS_RAW_KEEPALIVE", "")
                                # Default OFF: this uses nats-py internals (`_send_command`/`_flush_pending`) from a
                                # separate task and can introduce hard-to-debug races. Root already sends NATS PINGs to
                                # elicit hub->root traffic (PONG), and nats-py also has its own ping interval.
                                raw_keepalive_enabled = raw_keepalive_env.strip() == "1"
                            except Exception:
                                raw_keepalive_enabled = False
                            if raw_keepalive_enabled:
                                try:
                                    raw_keepalive_s = float(os.getenv("HUB_NATS_RAW_KEEPALIVE_S", "15") or "15")
                                except Exception:
                                    raw_keepalive_s = 15.0
                                if raw_keepalive_s < 5.0:
                                    raw_keepalive_s = 5.0

                                async def _raw_keepalive_loop() -> None:
                                    ping_cmd = b"PING\r\n"
                                    sent = 0
                                    while True:
                                        await asyncio.sleep(raw_keepalive_s)
                                        try:
                                            is_closed_attr = getattr(nc, "is_closed", None)
                                            is_closed = is_closed_attr() if callable(is_closed_attr) else bool(is_closed_attr)
                                            if is_closed:
                                                return
                                        except Exception:
                                            pass
                                        try:
                                            sc = getattr(nc, "_send_command", None)
                                            fp = getattr(nc, "_flush_pending", None)
                                            if callable(sc) and callable(fp):
                                                await sc(ping_cmd)
                                                # Ensure the frame actually hits the wire; otherwise some proxies/LBs
                                                # may still consider the connection idle and close it (1006/EOF).
                                                try:
                                                    await fp(force_flush=True)
                                                except TypeError:
                                                    try:
                                                        await fp(True)
                                                    except TypeError:
                                                        await fp()
                                            else:
                                                # Fallback: if internals changed, use public flush() to force outbound IO.
                                                flush = getattr(nc, "flush", None)
                                                if callable(flush):
                                                    try:
                                                        await flush(timeout=1.0)
                                                    except Exception:
                                                        pass
                                            sent += 1
                                            try:
                                                # Log early pings too: if we disconnect before reaching 10,
                                                # it is still useful to know whether we managed to send keepalives.
                                                if sent <= 3 and (os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace):
                                                    _rl_log(
                                                        "nats.raw_keepalive_first",
                                                        f"[hub-io] nats raw keepalive sent={sent} every_s={raw_keepalive_s:.1f}",
                                                        every_s=0.5,
                                                    )
                                                if (sent % 10) == 0 and (os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace):
                                                    _rl_log(
                                                        "nats.raw_keepalive",
                                                        f"[hub-io] nats raw keepalive sent={sent} every_s={raw_keepalive_s:.1f}",
                                                        every_s=5.0,
                                                    )
                                            except Exception:
                                                pass
                                        except Exception as e:
                                            try:
                                                if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                                    _rl_log(
                                                        "nats.raw_keepalive_err",
                                                        f"[hub-io] nats raw keepalive failed err={type(e).__name__}: {e}",
                                                        every_s=1.0,
                                                    )
                                            except Exception:
                                                pass
                                            # Keepalive is best-effort; connection supervisor will handle reconnects.
                                            pass

                                try:
                                    raw_keepalive_task = asyncio.create_task(_raw_keepalive_loop(), name="adaos-nats-raw-keepalive")
                                except Exception:
                                    raw_keepalive_task = None

                            # Track subscriptions explicitly. When the connection closes (or this task is cancelled),
                            # unsubscribing helps nats-py cancel internal `_wait_for_msgs()` tasks and avoids
                            # "Task was destroyed but it is pending!" warnings on reconnect/shutdown.
                            subs: list[Any] = []

                            async def _sub(subject: str, *, cb: Any):
                                sub = await nc.subscribe(subject, cb=cb)
                                subs.append(sub)
                                return sub

                            # Outbound bridge: local bus -> root NATS.
                            # This lets skills/router publish `tg.output.<bot>.chat.<chat_id>` and have
                            # the backend deliver it to Telegram, without requiring TG_BOT_TOKEN on the hub.
                            try:
                                setattr(self, "_tg_output_nats_nc", nc)
                            except Exception:
                                pass

                            # Drain outbox (replay replies produced while NATS was down/flapping).
                            try:
                                q = getattr(self, "_tg_output_pending", None)
                                if q:
                                    drained = 0
                                    max_drain = 200
                                    try:
                                        max_drain = int(os.getenv("HUB_TG_OUTBOX_DRAIN_MAX", "200") or "200")
                                    except Exception:
                                        max_drain = 200
                                    while q and (max_drain <= 0 or drained < max_drain):
                                        try:
                                            subj0, data0 = q[0]
                                        except Exception:
                                            break
                                        try:
                                            await nc.publish(str(subj0), bytes(data0))
                                            fp = getattr(nc, "_flush_pending", None)
                                            if callable(fp):
                                                await fp(force_flush=True)
                                            try:
                                                q.popleft()
                                            except Exception:
                                                pass
                                            drained += 1
                                        except Exception:
                                            break
                                    if drained and (hub_nats_verbose or trace):
                                        _rl_log("nats.outbox", f"[hub-io] tg outbox drained={drained}", every_s=1.0)
                            except Exception:
                                pass

                            try:
                                if not bool(getattr(self, "_tg_output_bridge_hooked", False)):

                                    def _on_local_output(ev: Event) -> None:
                                        try:
                                            subj = ev.type
                                            if not isinstance(subj, str) or not subj.startswith("tg.output."):
                                                return
                                            try:
                                                data = _json.dumps(ev.payload or {}, ensure_ascii=False).encode("utf-8")
                                            except Exception:
                                                data = b"{}"
                                            max_outbox = 200
                                            try:
                                                max_outbox = int(os.getenv("HUB_TG_OUTBOX_MAX", "200") or "200")
                                            except Exception:
                                                max_outbox = 200

                                            def _queue() -> None:
                                                try:
                                                    q = getattr(self, "_tg_output_pending", None)
                                                    if q is None:
                                                        q = deque()
                                                        setattr(self, "_tg_output_pending", q)
                                                    while max_outbox > 0 and len(q) >= max_outbox:
                                                        q.popleft()
                                                    q.append((subj, data))
                                                except Exception:
                                                    return

                                            nc2 = getattr(self, "_tg_output_nats_nc", None)
                                            if not nc2:
                                                _queue()
                                                return

                                            async def _publish_or_queue() -> None:
                                                try:
                                                    await nc2.publish(subj, data)
                                                    fp = getattr(nc2, "_flush_pending", None)
                                                    if callable(fp):
                                                        await fp(force_flush=True)
                                                except Exception:
                                                    _queue()

                                            try:
                                                loop = asyncio.get_running_loop()
                                                loop.create_task(_publish_or_queue())
                                            except RuntimeError:
                                                _queue()
                                        except Exception:
                                            return

                                    # Prefix subscription on LocalEventBus works as "starts with".
                                    core_bus.subscribe("tg.output.", _on_local_output)
                                    setattr(self, "_tg_output_bridge_hooked", True)
                            except Exception:
                                pass
                            subj = f"tg.input.{hub_id}"
                            subj_legacy = f"io.tg.in.{hub_id}.text"
                            if hub_nats_verbose or not hub_nats_quiet:
                                print(f"[hub-io] NATS subscribe {subj} and legacy {subj_legacy}")
                            else:
                                # In quiet mode we still want a single signal that we are connected, because
                                # troubleshooting "TG stops responding" depends on correlating with NATS flaps.
                                _rl_log(
                                    "nats.connected",
                                    f"[hub-io] nats connected ({connected_server or 'unknown'})",
                                    every_s=2.0,
                                )
                            # First successful connect after failures
                            _emit_up()
                            nats_last_ok_at = time.monotonic()
                            # Baseline for RX watchdog (updated by patched WebSocketTransport.readline()).
                            try:
                                tr = getattr(nc, "_transport", None)
                                if tr is not None and not hasattr(tr, "_adaos_last_rx_at"):
                                    setattr(tr, "_adaos_last_rx_at", time.monotonic())
                            except Exception:
                                pass

                            # Control channel: hub alias updates from backend
                            try:
                                ctl_alias = f"hub.control.{hub_id}.alias"

                                async def _ctl_alias_cb(msg):
                                    try:
                                        data = _json.loads(msg.data.decode("utf-8"))
                                    except Exception:
                                        data = {}
                                    alias = (data or {}).get("alias")
                                    if isinstance(alias, str) and alias:
                                        try:
                                            from adaos.services.capacity import _load_node_yaml as _load_node, _save_node_yaml as _save_node

                                            y = _load_node()
                                            n = y.get("nats") or {}
                                            n["alias"] = alias
                                            y["nats"] = n
                                            _save_node(y)
                                            try:
                                                self.ctx.bus.publish(Event(type="subnet.alias.changed", payload={"alias": alias, "subnet_id": hub_id}, source="io.nats"))
                                            except Exception:
                                                pass
                                            print(f"[hub-io] alias set via NATS: {alias}")
                                        except Exception:
                                            pass

                                await _sub(ctl_alias, cb=_ctl_alias_cb)
                                if hub_nats_verbose or not hub_nats_quiet:
                                    print(f"[hub-io] NATS subscribe control {ctl_alias}")
                            except Exception:
                                pass
                            break
                        except Exception as e:
                            # Optionally print per-attempt diagnostics when verbose
                            try:
                                if os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                    emsg = _explain_connect_error(e)
                                    print(f"[hub-io] NATS connect failed: {emsg}")
                                    try:
                                        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                                        print(tb.rstrip())
                                    except Exception:
                                        pass
                                else:
                                    if not (
                                        os.getenv("SILENCE_NATS_EOF", "0") == "1"
                                        and (type(e).__name__ == "UnexpectedEOF" or "unexpected eof" in str(e).lower())
                                    ):
                                        # Minimal single-line failure for non-EOF issues
                                        print(f"[hub-io] NATS connect failed: {_explain_connect_error(e)}")
                            except Exception:
                                pass
                            # One-time down message and bus event while offline
                            try:
                                _emit_down("connect_error", e)
                            except Exception:
                                pass
                            # On failure, keep retrying with backoff; candidates are rebuilt each attempt.
                            await asyncio.sleep(backoff)
                            backoff = min(backoff * 2.0, 30.0)

                    async def cb(msg):
                        if trace:
                            try:
                                _rl_log("nats.msg", f"[hub-io] nats recv subject={getattr(msg, 'subject', '')} bytes={len(getattr(msg, 'data', b'') or b'')}", every_s=0.2)
                            except Exception:
                                pass
                        try:
                            data = _json.loads(msg.data.decode("utf-8"))
                        except Exception:
                            data = {}
                        try:
                            # Media fetch: if event includes telegram media, download to local cache and annotate path
                            p = (data or {}).get("payload") or {}
                            typ = p.get("type") or (data.get("type") if isinstance(data.get("type"), str) else None)
                            bot_id = p.get("bot_id") or data.get("bot_id") or ""
                            file_id = p.get("file_id") if isinstance(p, dict) else None
                            if not file_id and isinstance(p, dict):
                                file_id = p.get("payload", {}).get("file_id") if isinstance(p.get("payload"), dict) else None
                            media_path = None
                            if isinstance(typ, str) and file_id and bot_id and typ in ("photo", "document", "audio", "voice"):
                                base = self.ctx.settings.api_base.rstrip("/")
                                token = os.getenv("ADAOS_TOKEN", "")
                                url = f"{base}/internal/tg/file?bot_id={bot_id}&file_id={file_id}"
                                cache_dir = self.ctx.paths.cache_dir()
                                cache_dir.mkdir(parents=True, exist_ok=True)
                                import urllib.request as _ureq
                                import uuid as _uuid
                                import mimetypes as _mtypes
                                # `urllib` is blocking; run the download and file write in a worker thread so it
                                # doesn't stall the hub event loop (and therefore NATS keepalives).
                                def _download() -> str:
                                    req = _ureq.Request(url, headers={"X-AdaOS-Token": token})
                                    with _ureq.urlopen(req, timeout=20) as resp:
                                        # Prefer filename from header; fallback to Content-Disposition; then use type
                                        fname = resp.headers.get("X-File-Name") or ""
                                        if not fname:
                                            cd = resp.headers.get("Content-Disposition") or ""
                                            try:
                                                import cgi as _cgi

                                                _val, _params = _cgi.parse_header(cd)
                                                fname = _params.get("filename") or ""
                                            except Exception:
                                                fname = ""
                                        if fname:
                                            import os as _os

                                            fname = _os.path.basename(fname)
                                        else:
                                            # fallback to type-based extension
                                            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
                                            ext = _mtypes.guess_extension(ctype) or ""
                                            fname = f"tg_{_uuid.uuid4().hex}{ext}"
                                        dest = cache_dir / fname
                                        with open(dest, "wb") as out:
                                            out.write(resp.read())
                                    return str(dest)

                                media_path = await asyncio.to_thread(_download)
                                # annotate
                                if isinstance(p, dict):
                                    if isinstance(p.get("payload"), dict):
                                        p["payload"]["file_path"] = media_path
                                    else:
                                        p["file_path"] = media_path
                                data["payload"] = p
                        except Exception:
                            pass
                        try:
                            self.ctx.bus.publish(Event(type=subj, payload=data, source="io.nats", ts=time.time()))
                        except Exception:
                            pass

                    await _sub(subj, cb=cb)

                    # Browser<->Hub routing over NATS (root proxy fallback).
                    # Root publishes `route.to_hub.<key>` where key is "<hub_id>--<conn_id|http--req_id>" (no dots).
                    # Hub responds on `route.to_browser.<same-key>`.
                    try:
                        # Optional dependency: if `websockets` is missing, keep HTTP proxy working
                        # and gracefully deny WS tunnel opens.
                        websockets_mod = None
                        try:
                            import websockets as _websockets  # type: ignore

                            websockets_mod = _websockets
                        except Exception:
                            websockets_mod = None

                        tunnels: dict[str, dict[str, Any]] = {}
                        tunnel_tasks: dict[str, asyncio.Task] = {}
                        pending_chunks: dict[str, dict[str, Any]] = {}
                        MAX_CHUNK_RAW = 300_000

                        _route_verbose = os.getenv("HUB_ROUTE_VERBOSE", "0") == "1"
                        _route_diag = _route_verbose or os.getenv("HUB_ROUTE_DIAG", "0") == "1"
                        # Tx logs are extremely noisy (one line per request / response). Keep them separately gated.
                        _route_tx_verbose = os.getenv("HUB_ROUTE_TX_VERBOSE", "0") == "1"
                        # In WS-proxied NATS setups, route replies can sit in local buffers and root times out
                        # waiting for `route.to_browser.*`. Keep fast drain enabled by default.
                        _route_force_flush = os.getenv("HUB_ROUTE_FORCE_FLUSH", "1") == "1"
                        try:
                            _route_send_timeout_s = float(os.getenv("HUB_ROUTE_SEND_TIMEOUT_S", "2.0") or "2.0")
                        except Exception:
                            _route_send_timeout_s = 2.0
                        try:
                            _route_upstream_ws_send_timeout_s = float(
                                os.getenv("HUB_ROUTE_UPSTREAM_WS_SEND_TIMEOUT_S", "2.0") or "2.0"
                            )
                        except Exception:
                            _route_upstream_ws_send_timeout_s = 2.0
                        try:
                            _route_flush_timeout_s = float(os.getenv("HUB_ROUTE_FLUSH_TIMEOUT_S", "1.0") or "1.0")
                        except Exception:
                            _route_flush_timeout_s = 1.0

                        # Optional probe mitigation: resend inline probe replies after short delays.
                        # Useful when NATS-over-WS intermittently drops a single PUB frame and Root times out.
                        _route_probe_resend_delays_s: list[float] = []
                        try:
                            raw_delays = str(os.getenv("HUB_ROUTE_PROBE_RESEND_S", "") or "").strip()
                            if raw_delays:
                                seen_delays: set[float] = set()
                                for it in raw_delays.split(","):
                                    it = it.strip()
                                    if not it:
                                        continue
                                    try:
                                        d = float(it)
                                    except Exception:
                                        continue
                                    if d <= 0:
                                        continue
                                    # Avoid runaway schedules.
                                    if d > 10.0:
                                        d = 10.0
                                    if d in seen_delays:
                                        continue
                                    seen_delays.add(d)
                                    _route_probe_resend_delays_s.append(d)
                        except Exception:
                            _route_probe_resend_delays_s = []
                        if _route_probe_resend_delays_s and (_route_verbose or _route_tx_verbose):
                            try:
                                _rl_log(
                                    "hub-route.probe_resend_cfg",
                                    f"[hub-route] probe resend delays_s={_route_probe_resend_delays_s}",
                                    every_s=60.0,
                                )
                            except Exception:
                                pass

                        def _route_nc_diag() -> str:
                            try:
                                tr = getattr(nc, "_transport", None)
                                ws = getattr(tr, "_ws", None) if tr is not None else None
                                ws_closed = getattr(ws, "closed", None) if ws is not None else None
                                ws_close_code = getattr(ws, "close_code", None) if ws is not None else None
                                ws_close_reason = getattr(ws, "close_reason", None) if ws is not None else None
                                ws_exc = None
                                try:
                                    exf = getattr(ws, "exception", None)
                                    if callable(exf):
                                        ws_exc = exf()
                                except Exception:
                                    ws_exc = None
                                ws_proto = None
                                try:
                                    ws_proto = getattr(ws, "protocol", None) if ws is not None else None
                                except Exception:
                                    ws_proto = None
                                try:
                                    if not ws_proto and ws is not None and getattr(ws, "_response", None) is not None:
                                        ws_proto = ws._response.headers.get("Sec-WebSocket-Protocol")  # type: ignore[attr-defined]
                                except Exception:
                                    ws_proto = ws_proto or None

                                last_rx_ago_s = None
                                last_tx_ago_s = None
                                try:
                                    last_rx_at = getattr(tr, "_adaos_last_rx_at", None) if tr is not None else None
                                    last_tx_at = getattr(tr, "_adaos_last_tx_at", None) if tr is not None else None
                                    if isinstance(last_rx_at, (int, float)):
                                        last_rx_ago_s = round(time.monotonic() - float(last_rx_at), 3)
                                    if isinstance(last_tx_at, (int, float)):
                                        last_tx_ago_s = round(time.monotonic() - float(last_tx_at), 3)
                                except Exception:
                                    last_rx_ago_s = last_rx_ago_s or None
                                    last_tx_ago_s = last_tx_ago_s or None

                                pending_data_size = getattr(nc, "_pending_data_size", None)
                                pings_outstanding = getattr(nc, "_pings_outstanding", None)
                                pongs_q = None
                                try:
                                    pongs = getattr(nc, "_pongs", None)
                                    if isinstance(pongs, list):
                                        pongs_q = len(pongs)
                                except Exception:
                                    pongs_q = None
                                return (
                                    f"ws_closed={ws_closed} close_code={ws_close_code} close_reason={ws_close_reason} "
                                    f"ws_exc={ws_exc} ws_proto={ws_proto} "
                                    f"last_rx_ago_s={last_rx_ago_s} last_tx_ago_s={last_tx_ago_s} "
                                    f"pending_data_size={pending_data_size} pings_outstanding={pings_outstanding} pongs_q={pongs_q}"
                                )
                            except Exception:
                                return ""

                        async def _route_reply(key: str, payload: dict[str, Any]) -> None:
                            try:
                                try:
                                    await asyncio.wait_for(
                                        nc.publish(
                                            f"route.to_browser.{key}",
                                            _json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                        ),
                                        timeout=max(0.1, float(_route_send_timeout_s)),
                                    )
                                except asyncio.TimeoutError:
                                    raise RuntimeError("publish timeout")
                                # Ensure the reply is actually flushed quickly; otherwise Root may time out
                                # waiting on `route.to_browser.<key>` (especially over websocket-proxied NATS).
                                try:
                                    t = (payload or {}).get("t")
                                    if _route_force_flush and t in ("http_resp", "close"):
                                        # Fast-drain pending bytes without relying on NATS PING/PONG.
                                        # This avoids `flush()` (which can time out when PONGs are flaky behind WS proxies).
                                        try:
                                            tout = max(0.1, float(_route_flush_timeout_s))
                                        except Exception:
                                            tout = 1.0
                                        flush_err = None
                                        flush_started = time.monotonic()
                                        fp = getattr(nc, "_flush_pending", None)
                                        if callable(fp):
                                            try:
                                                try:
                                                    await asyncio.wait_for(fp(force_flush=True), timeout=tout)
                                                except TypeError:
                                                    try:
                                                        await asyncio.wait_for(fp(True), timeout=tout)
                                                    except TypeError:
                                                        await asyncio.wait_for(fp(), timeout=tout)
                                            except Exception as e:
                                                flush_err = e
                                        else:
                                            # Fallback: old clients might not have `_flush_pending`.
                                            try:
                                                await nc.flush(timeout=tout)
                                            except Exception as e:
                                                flush_err = e
                                        flush_took_s = time.monotonic() - flush_started
                                        if flush_err is not None:
                                            try:
                                                _rl_log(
                                                    "hub-route.flush_fail",
                                                    f"[hub-route] flush failed t={t} key={key}: {type(flush_err).__name__}: {flush_err} {_route_nc_diag()}",
                                                    every_s=1.0,
                                                )
                                            except Exception:
                                                pass
                                        elif flush_took_s >= max(0.5, float(tout) * 0.9) and t == "http_resp":
                                            # Slow flush can still cause root timeouts even if publish succeeds.
                                            try:
                                                _rl_log(
                                                    "hub-route.flush_slow",
                                                    f"[hub-route] flush slow took_s={flush_took_s:.3f} t={t} key={key} {_route_nc_diag()}",
                                                    every_s=1.0,
                                                )
                                            except Exception:
                                                pass
                                        if _route_tx_verbose:
                                            try:
                                                print(f"[hub-route] tx {t} key={key}")
                                            except Exception:
                                                pass
                                except Exception:
                                    pass
                            except Exception as e:
                                try:
                                    t0 = (payload or {}).get("t")
                                except Exception:
                                    t0 = None
                                # Do not silently drop probe replies: Root will time out and surface `hub_unreachable`.
                                if t0 in ("http_resp", "close") or _route_verbose:
                                    try:
                                        _rl_log(
                                            "hub-route.publish_fail",
                                            f"[hub-route] publish to_browser failed t={t0} key={key}: {type(e).__name__}: {e} {_route_nc_diag()}",
                                            every_s=1.0,
                                        )
                                    except Exception:
                                        pass

                        def _hub_key_match(key: str) -> bool:
                            # key is "<hub_id>--..."
                            try:
                                return isinstance(key, str) and key.startswith(f"{hub_id}--")
                            except Exception:
                                return False

                        async def _tunnel_reader(key: str, ws) -> None:
                            try:
                                async for msg in ws:
                                    if isinstance(msg, (bytes, bytearray)):
                                        raw = bytes(msg)
                                        if len(raw) > MAX_CHUNK_RAW:
                                            cid = f"c_{uuid.uuid4().hex}"
                                            total = (len(raw) + MAX_CHUNK_RAW - 1) // MAX_CHUNK_RAW
                                            for idx in range(total):
                                                chunk = raw[idx * MAX_CHUNK_RAW : (idx + 1) * MAX_CHUNK_RAW]
                                                await _route_reply(
                                                    key,
                                                    {
                                                        "t": "chunk",
                                                        "id": cid,
                                                        "kind": "bin",
                                                        "idx": idx,
                                                        "total": total,
                                                        "data_b64": base64.b64encode(chunk).decode("ascii"),
                                                    },
                                                )
                                        else:
                                            await _route_reply(
                                                key,
                                                {
                                                    "t": "frame",
                                                    "kind": "bin",
                                                    "data_b64": base64.b64encode(raw).decode("ascii"),
                                                },
                                            )
                                    else:
                                        text = str(msg)
                                        if len(text) > MAX_CHUNK_RAW:
                                            cid = f"c_{uuid.uuid4().hex}"
                                            parts = [text[i : i + MAX_CHUNK_RAW] for i in range(0, len(text), MAX_CHUNK_RAW)]
                                            for idx, part in enumerate(parts):
                                                await _route_reply(
                                                    key,
                                                    {"t": "chunk", "id": cid, "kind": "text", "idx": idx, "total": len(parts), "data": part},
                                                )
                                        else:
                                            await _route_reply(key, {"t": "frame", "kind": "text", "data": text})
                            except Exception:
                                pass
                            finally:
                                try:
                                    await _route_reply(key, {"t": "close"})
                                except Exception:
                                    pass
                                tunnels.pop(key, None)
                                t = tunnel_tasks.pop(key, None)
                                try:
                                    if t:
                                        t.cancel()
                                except Exception:
                                    pass
                                try:
                                    # clear pending chunks for this connection
                                    for pid in [pid for pid, st in list(pending_chunks.items()) if st.get("key") == key]:
                                        pending_chunks.pop(pid, None)
                                except Exception:
                                    pass
                                try:
                                    await ws.close()
                                except Exception:
                                    pass

                        async def _route_cb(msg) -> None:
                            key = ""
                            is_http_key = False
                            try:
                                subject = str(getattr(msg, "subject", "") or "")
                                parts = subject.split(".", 2)
                                # route.to_hub.<key>
                                if len(parts) < 3:
                                    if _route_diag:
                                        try:
                                            _rl_log(
                                                "hub-route.drop_subject",
                                                f"[hub-route] drop: bad subject={subject!s}",
                                                every_s=2.0,
                                            )
                                        except Exception:
                                            pass
                                    return
                                key = parts[2]
                                is_http_key = isinstance(key, str) and "--http--" in key
                                if not _hub_key_match(key):
                                    if _route_diag:
                                        try:
                                            _rl_log(
                                                "hub-route.drop_key",
                                                f"[hub-route] drop: key mismatch subject={subject!s} key={key!s} expected_prefix={hub_id}--",
                                                every_s=2.0,
                                            )
                                        except Exception:
                                            pass
                                    return

                                try:
                                    raw = bytes(getattr(msg, "data", b"") or b"")
                                except Exception:
                                    raw = b""
                                try:
                                    data = _json.loads(raw.decode("utf-8"))
                                except Exception as e:
                                    if _route_diag:
                                        try:
                                            _rl_log(
                                                "hub-route.drop_json",
                                                f"[hub-route] drop: invalid json key={key} bytes={len(raw)} err={type(e).__name__}: {e}",
                                                every_s=2.0,
                                            )
                                        except Exception:
                                            pass
                                    # Avoid systematic `hub_unreachable` timeouts for HTTP keys.
                                    try:
                                        if is_http_key:
                                            await _route_reply(
                                                key,
                                                {"t": "http_resp", "status": 502, "headers": {}, "body_b64": "", "truncated": False, "err": "invalid_json"},
                                            )
                                    except Exception:
                                        pass
                                    return
                                if not isinstance(data, dict):
                                    if _route_diag:
                                        try:
                                            _rl_log(
                                                "hub-route.drop_payload",
                                                f"[hub-route] drop: unexpected payload type key={key} type={type(data).__name__}",
                                                every_s=2.0,
                                            )
                                        except Exception:
                                            pass
                                    try:
                                        if is_http_key:
                                            await _route_reply(
                                                key,
                                                {"t": "http_resp", "status": 502, "headers": {}, "body_b64": "", "truncated": False, "err": "invalid_payload"},
                                            )
                                    except Exception:
                                        pass
                                    return
                                t = (data or {}).get("t")
                                if not isinstance(t, str) or not t:
                                    if _route_diag:
                                        try:
                                            _rl_log(
                                                "hub-route.drop_missing_t",
                                                f"[hub-route] drop: missing t key={key}",
                                                every_s=2.0,
                                            )
                                        except Exception:
                                            pass
                                    try:
                                        if is_http_key:
                                            await _route_reply(
                                                key,
                                                {"t": "http_resp", "status": 502, "headers": {}, "body_b64": "", "truncated": False, "err": "missing_t"},
                                            )
                                    except Exception:
                                        pass
                                    return
                                if _route_verbose:
                                    try:
                                        if t == "http":
                                            _m = str((data or {}).get("method") or "GET").upper()
                                            _p = str((data or {}).get("path") or "")
                                            if _p not in ("/api/node/status", "/api/ping", "/healthz"):
                                                print(f"[hub-route] rx http key={key} {_m} {_p}")
                                            else:
                                                try:
                                                    _rl_log(
                                                        "hub-route.rx_http_probe",
                                                        f"[hub-route] rx http probe key={key} {_m} {_p}",
                                                        every_s=5.0,
                                                    )
                                                except Exception:
                                                    pass
                                        elif t == "open":
                                            _p = str((data or {}).get("path") or "")
                                            if _p not in ("/api/node/status", "/api/ping"):
                                                print(f"[hub-route] rx open key={key} path={_p}")
                                        elif t == "close":
                                            print(f"[hub-route] rx close key={key}")
                                        else:
                                            # Frames are extremely noisy; enable explicitly when debugging.
                                            if t == "frame" and os.getenv("ROUTE_PROXY_FRAME_VERBOSE", "0") != "1":
                                                pass
                                            else:
                                                print(f"[hub-route] rx t={t} key={key}")
                                    except Exception:
                                        pass

                                if t == "open":
                                    # Open a local WS to the hub server and start pumping frames.
                                    if websockets_mod is None:
                                        await _route_reply(key, {"t": "close", "err": "websockets_unavailable"})
                                        return
                                    path = str((data or {}).get("path") or "/ws")
                                    query = str((data or {}).get("query") or "")
                                    # Local hub server is always reachable inside the hub machine/container.
                                    try:
                                        from adaos.services.node_config import load_config

                                        cfg = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
                                        # Prefer the actual base URL the hub is serving on (set by `adaos api` / dev),
                                        # because the hub may not be reachable on the default 8777 (e.g. sentinel off).
                                        base_http = (
                                            os.getenv("ADAOS_SELF_BASE_URL")
                                            or str(getattr(cfg, "hub_url", None) or "")
                                            or "http://127.0.0.1:8777"
                                        ).rstrip("/")
                                        # Do not use 0.0.0.0/:: as client destinations.
                                        base_http = base_http.replace("://0.0.0.0:", "://127.0.0.1:").replace("://[::]:", "://127.0.0.1:")
                                        base_ws = base_http.replace("http://", "ws://").replace("https://", "wss://")
                                        token_local = getattr(cfg, "token", None) or os.getenv("ADAOS_TOKEN", "") or None
                                    except Exception:
                                        base_ws = "ws://127.0.0.1:8777"
                                        token_local = os.getenv("ADAOS_TOKEN", "") or None
                                    # Translate root-proxy JWT token into local hub token for upstream hub WS auth.
                                    # Local hub expects `token=<X-AdaOS-Token>`; forwarding the session JWT makes the
                                    # hub close immediately and the browser retries endlessly.
                                    try:
                                        from urllib.parse import parse_qs, urlencode

                                        if query.startswith("?"):
                                            q = parse_qs(query[1:], keep_blank_values=True)
                                        else:
                                            q = parse_qs(query, keep_blank_values=True)
                                        if token_local:
                                            q["token"] = [str(token_local)]
                                        else:
                                            # If we don't have a local token, do not forward the root session JWT.
                                            q.pop("token", None)
                                        query = "?" + urlencode(q, doseq=True) if q else ""
                                    except Exception:
                                        pass
                                    url = f"{base_ws}{path}{query}"
                                    if _route_verbose:
                                        try:
                                            print(f"[hub-route] open upstream url={url}")
                                        except Exception:
                                            pass
                                    # Ensure we don't leak multiple opens for same key.
                                    try:
                                        old = tunnels.get(key)
                                        if old and old.get("ws"):
                                            try:
                                                await old["ws"].close()
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                                    try:
                                        # Yjs sync frames can exceed 1 MiB; do not enforce a small client-side cap.
                                        try:
                                            ws_connect_timeout_s = float(
                                                os.getenv("HUB_ROUTE_UPSTREAM_WS_CONNECT_TIMEOUT_S", "2.5") or "2.5"
                                            )
                                        except Exception:
                                            ws_connect_timeout_s = 2.5
                                        if ws_connect_timeout_s < 0.1:
                                            ws_connect_timeout_s = 0.1
                                        ws = await asyncio.wait_for(
                                            websockets_mod.connect(url, max_size=None),
                                            timeout=ws_connect_timeout_s,
                                        )
                                    except Exception as e:
                                        await _route_reply(key, {"t": "close", "err": str(e)})
                                        return
                                    tunnels[key] = {"ws": ws, "url": url}
                                    tunnel_tasks[key] = asyncio.create_task(_tunnel_reader(key, ws), name=f"hub-route-{key}")
                                    return

                                if t == "close":
                                    rec = tunnels.pop(key, None)
                                    task = tunnel_tasks.pop(key, None)
                                    try:
                                        if task:
                                            task.cancel()
                                    except Exception:
                                        pass
                                    try:
                                        if rec and rec.get("ws"):
                                            await rec["ws"].close()
                                    except Exception:
                                        pass
                                    return

                                if t == "frame":
                                    rec = tunnels.get(key)
                                    ws = rec.get("ws") if isinstance(rec, dict) else None
                                    if not ws:
                                        return
                                    kind = (data or {}).get("kind")
                                    if kind == "bin":
                                        b64 = (data or {}).get("data_b64")
                                        if isinstance(b64, str) and b64:
                                            try:
                                                await asyncio.wait_for(
                                                    ws.send(base64.b64decode(b64.encode("ascii"))),
                                                    timeout=max(0.1, float(_route_upstream_ws_send_timeout_s)),
                                                )
                                            except Exception as e:
                                                if _route_verbose:
                                                    try:
                                                        print(f"[hub-route] ws.send(bin) failed key={key}: {type(e).__name__}: {e}")
                                                    except Exception:
                                                        pass
                                    else:
                                        txt = (data or {}).get("data")
                                        if isinstance(txt, str):
                                            try:
                                                await asyncio.wait_for(
                                                    ws.send(txt),
                                                    timeout=max(0.1, float(_route_upstream_ws_send_timeout_s)),
                                                )
                                            except Exception as e:
                                                if _route_verbose:
                                                    try:
                                                        print(f"[hub-route] ws.send(text) failed key={key}: {type(e).__name__}: {e}")
                                                    except Exception:
                                                        pass
                                    return
                                
                                if t == "chunk":
                                    rec = tunnels.get(key)
                                    ws = rec.get("ws") if isinstance(rec, dict) else None
                                    if not ws:
                                        return
                                    cid = (data or {}).get("id")
                                    idx = int((data or {}).get("idx") or 0)
                                    total = int((data or {}).get("total") or 0)
                                    kind = "text" if (data or {}).get("kind") == "text" else "bin"
                                    if not isinstance(cid, str) or not cid or total <= 0 or idx < 0 or idx >= total:
                                        return
                                    st = pending_chunks.get(cid)
                                    if not st:
                                        st = {"key": key, "kind": kind, "total": total, "parts": [None] * total}
                                        pending_chunks[cid] = st
                                    if st.get("key") != key or st.get("kind") != kind or int(st.get("total") or 0) != total:
                                        return
                                    parts = st.get("parts")
                                    if not isinstance(parts, list) or len(parts) != total:
                                        st["parts"] = [None] * total
                                        parts = st["parts"]
                                    if kind == "bin":
                                        b64 = (data or {}).get("data_b64")
                                        if not isinstance(b64, str):
                                            return
                                        parts[idx] = base64.b64decode(b64.encode("ascii"))
                                    else:
                                        txt = (data or {}).get("data")
                                        if not isinstance(txt, str):
                                            return
                                        parts[idx] = txt
                                    if any(p is None for p in parts):
                                        return
                                    pending_chunks.pop(cid, None)
                                    try:
                                        if kind == "bin":
                                            blob = b"".join([p for p in parts if isinstance(p, (bytes, bytearray))])
                                            await asyncio.wait_for(
                                                ws.send(blob),
                                                timeout=max(0.1, float(_route_upstream_ws_send_timeout_s)),
                                            )
                                        else:
                                            await asyncio.wait_for(
                                                ws.send("".join([p for p in parts if isinstance(p, str)])),
                                                timeout=max(0.1, float(_route_upstream_ws_send_timeout_s)),
                                            )
                                    except Exception as e:
                                        if _route_verbose:
                                            try:
                                                print(f"[hub-route] ws.send(chunked) failed key={key}: {type(e).__name__}: {e}")
                                            except Exception:
                                                pass
                                    return

                                if t == "http":
                                    method = str((data or {}).get("method") or "GET").upper()
                                    path = str((data or {}).get("path") or "/api/ping")
                                    # Be tolerant: root might send trailing slashes.
                                    path_norm = (path.rstrip("/") or "/") if isinstance(path, str) else "/"
                                    search = str((data or {}).get("search") or "")
                                    headers = (data or {}).get("headers") or {}
                                    body_b64 = (data or {}).get("body_b64")

                                    # Root continuously probes `/api/node/status` (and `/api/ping`) with a short timeout
                                    # to decide whether the hub is reachable. When the hub is under load (YJS/WebRTC
                                    # init) the local HTTP stack may respond slowly, and root will surface
                                    # `hub_unreachable` / `yjs_sync_timeout`.
                                    #
                                    # Return these probe endpoints inline (no local HTTP) so the browser can log in
                                    # even when the hub API is busy.
                                    try:
                                        if method in ("GET", "HEAD") and path_norm in ("/api/node/status", "/api/ping", "/healthz"):
                                            if path_norm == "/api/node/status":
                                                try:
                                                    cfg = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
                                                except Exception:
                                                    cfg = load_config(ctx=self.ctx)
                                                payload0 = {
                                                    "node_id": str(getattr(cfg, "node_id", "") or ""),
                                                    "subnet_id": str(getattr(cfg, "subnet_id", "") or ""),
                                                    "role": str(getattr(cfg, "role", "") or ""),
                                                    "ready": bool(is_ready()),
                                                }
                                            else:
                                                payload0 = {"ok": True, "ts": time.time()}
                                            raw = _json.dumps(payload0, ensure_ascii=False).encode("utf-8")
                                            resp = {
                                                "t": "http_resp",
                                                "status": 200,
                                                "headers": {"content-type": "application/json"},
                                                "body_b64": base64.b64encode(raw).decode("ascii"),
                                                "truncated": False,
                                            }
                                            try:
                                                await _route_reply(key, resp)
                                            except Exception:
                                                pass
                                            if _route_probe_resend_delays_s:
                                                for delay_s in _route_probe_resend_delays_s:
                                                    async def _resend(delay_s: float = float(delay_s)) -> None:
                                                        try:
                                                            await asyncio.sleep(max(0.0, delay_s))
                                                            await _route_reply(key, resp)
                                                            if _route_tx_verbose or _route_verbose:
                                                                try:
                                                                    _rl_log(
                                                                        "hub-route.probe_resend",
                                                                        f"[hub-route] http probe resend delay_s={delay_s} key={key}",
                                                                        every_s=1.0,
                                                                    )
                                                                except Exception:
                                                                    pass
                                                        except Exception:
                                                            return

                                                    try:
                                                        asyncio.create_task(
                                                            _resend(),
                                                            name=f"hub-route-probe-resend-{key[-8:]}-{int(delay_s * 1000)}",
                                                        )
                                                    except Exception:
                                                        pass
                                            try:
                                                _rl_log(
                                                    "hub-route.inline_probe",
                                                    f"[hub-route] http inline ok path={path_norm} key={key}",
                                                    every_s=5.0,
                                                )
                                            except Exception:
                                                pass
                                            return
                                    except Exception:
                                        pass

                                    def _do_http() -> dict[str, Any]:
                                        try:
                                            import requests  # type: ignore

                                            try:
                                                from adaos.services.node_config import load_config

                                                cfg = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
                                                # IMPORTANT: Route-proxy HTTP requests must target the local hub instance,
                                                # not the public Root proxy URL that might be stored in node.yaml as hub_url.
                                                env_base = (
                                                    os.getenv("ADAOS_SELF_BASE_URL")
                                                    or os.getenv("ADAOS_BASE")
                                                    or os.getenv("ADAOS_API_BASE")
                                                    or ""
                                                ).strip()
                                                cfg_base = str(getattr(cfg, "hub_url", None) or "").strip()

                                                def _is_local_base(url: str) -> bool:
                                                    try:
                                                        from urllib.parse import urlparse

                                                        u = urlparse(url)
                                                        host = (u.hostname or "").lower()
                                                        return host in ("127.0.0.1", "localhost")
                                                    except Exception:
                                                        return False

                                                bases: list[str] = []
                                                if env_base:
                                                    bases.append(env_base.rstrip("/"))
                                                if cfg_base and _is_local_base(cfg_base):
                                                    bases.append(cfg_base.rstrip("/"))
                                                # Prefer direct core port, then sentinel gateway.
                                                bases.extend(["http://127.0.0.1:8778", "http://127.0.0.1:8777"])
                                                # Deduplicate while preserving order.
                                                seen_bases: set[str] = set()
                                                bases = [b for b in bases if (b not in seen_bases and not seen_bases.add(b))]
                                                token_local = getattr(cfg, "token", None) or os.getenv("ADAOS_TOKEN", "") or None
                                            except Exception:
                                                bases = ["http://127.0.0.1:8778", "http://127.0.0.1:8777"]
                                                token_local = os.getenv("ADAOS_TOKEN", "") or None

                                            # Add optional target/core port fallback for local setups.
                                            try:
                                                from urllib.parse import urlparse

                                                u0 = urlparse(bases[0])
                                                h0 = u0.hostname or "127.0.0.1"
                                                p0 = u0.port
                                                scheme0 = u0.scheme or "http"
                                                alt_port_raw = os.getenv("ADAOS_TARGET_PORT") or os.getenv("ADAOS_CORE_PORT") or ""
                                                alt_port = int(alt_port_raw) if alt_port_raw.strip() else 8788
                                                if (p0 in (None, 8777)) and alt_port and alt_port != p0:
                                                    bases.append(f"{scheme0}://{h0}:{alt_port}")
                                            except Exception:
                                                pass

                                            url = f"{bases[0]}{path}{search}"
                                            if _route_verbose and path not in ("/api/node/status", "/api/ping"):
                                                try:
                                                    print(f"[hub-route] http upstream url={url}")
                                                except Exception:
                                                    pass
                                            body = None
                                            if isinstance(body_b64, str) and body_b64:
                                                try:
                                                    body = base64.b64decode(body_b64.encode("ascii"))
                                                except Exception:
                                                    body = None
                                            # Minimal header allowlist.
                                            h2: dict[str, str] = {}
                                            if token_local:
                                                h2["X-AdaOS-Token"] = str(token_local)
                                            if isinstance(headers, dict):
                                                ct = headers.get("content-type") or headers.get("Content-Type")
                                                if isinstance(ct, str) and ct:
                                                    h2["Content-Type"] = ct
                                            # Do not inherit HTTP(S)_PROXY environment from the host/container:
                                            # local hub calls must stay local, otherwise they can hang on a proxy.
                                            sess = requests.Session()
                                            try:
                                                sess.trust_env = False
                                            except Exception:
                                                pass
                                            last_exc: Exception | None = None
                                            resp = None
                                            for base in bases:
                                                url_try = f"{base}{path}{search}"
                                                try:
                                                    # Root times out fairly quickly while waiting for route.to_browser.* replies.
                                                    # Keep local proxy attempts short to avoid systematic timeouts.
                                                    is_probe = path in ("/api/node/status", "/api/ping", "/healthz")
                                                    timeout = (0.5, 1.2) if is_probe else (1.5, 2.5)
                                                    resp = sess.request(method, url_try, data=body, headers=h2, timeout=timeout)
                                                    last_exc = None
                                                    break
                                                except Exception as e:
                                                    last_exc = e
                                                    if _route_verbose:
                                                        try:
                                                            print(f"[hub-route] http upstream failed url={url_try}: {type(e).__name__}: {e}")
                                                        except Exception:
                                                            pass
                                            if resp is None:
                                                raise last_exc or RuntimeError("http upstream failed")
                                            raw = resp.content or b""
                                            limit = 2 * 1024 * 1024
                                            truncated = len(raw) > limit
                                            if truncated:
                                                raw = raw[:limit]
                                            out_headers: dict[str, str] = {}
                                            try:
                                                cth = resp.headers.get("content-type")
                                                if cth:
                                                    out_headers["content-type"] = cth
                                            except Exception:
                                                pass
                                            return {
                                                "t": "http_resp",
                                                "status": int(resp.status_code),
                                                "headers": out_headers,
                                                "body_b64": base64.b64encode(raw).decode("ascii"),
                                                "truncated": truncated,
                                            }
                                        except Exception as e:
                                            return {"t": "http_resp", "status": 502, "headers": {}, "body_b64": "", "err": str(e)}

                                    resp = await asyncio.to_thread(_do_http)
                                    try:
                                        await _route_reply(key, resp)
                                    except Exception:
                                        pass
                                    return
                                # Unknown route message type: for HTTP keys, reply with an error so Root does not time out.
                                try:
                                    if is_http_key:
                                        await _route_reply(
                                            key,
                                            {
                                                "t": "http_resp",
                                                "status": 502,
                                                "headers": {},
                                                "body_b64": "",
                                                "truncated": False,
                                                "err": f"unsupported_t:{t}",
                                            },
                                        )
                                except Exception:
                                    pass
                                return
                            except Exception as e:
                                if _route_verbose:
                                    try:
                                        print(f"[hub-route] handler failed key={key}: {type(e).__name__}: {e}")
                                    except Exception:
                                        pass
                                # Avoid pure timeouts for HTTP keys; surface an error response instead.
                                try:
                                    if is_http_key and key:
                                        await _route_reply(
                                            key,
                                            {
                                                "t": "http_resp",
                                                "status": 502,
                                                "headers": {},
                                                "body_b64": "",
                                                "truncated": False,
                                                "err": f"handler_failed:{type(e).__name__}",
                                            },
                                        )
                                except Exception:
                                    pass
                                return

                        route_sub = await _sub("route.to_hub.*", cb=_route_cb)
                        if hub_nats_verbose or not hub_nats_quiet:
                            print("[hub-io] NATS subscribe route.to_hub.* (hub route proxy)")
                    except Exception as e:
                        # Do not fail the whole IO stack: this is an optional fallback used only when
                        # browser connects through Root (api.inimatic.com) and needs a NATS tunnel.
                        try:
                            if os.getenv("HUB_ROUTE_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                                print(f"[hub-io] NATS route proxy init failed: {type(e).__name__}: {e}")
                                try:
                                    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                                    print(tb.rstrip())
                                except Exception:
                                    pass
                            else:
                                print(f"[hub-io] NATS route proxy disabled: {type(e).__name__}: {e}")
                        except Exception:
                            pass

                    # Optional compatibility: also listen to additional hub aliases if explicitly configured
                    try:
                        aliases_env = os.getenv("HUB_INPUT_ALIASES", "")
                        aliases: List[str] = [a.strip() for a in aliases_env.split(",") if a.strip()]
                        seen = set([hub_id])
                        for aid in aliases:
                            if aid in seen:
                                continue
                            seen.add(aid)
                            alt = f"tg.input.{aid}"
                            if hub_nats_verbose or not hub_nats_quiet:
                                print(f"[hub-io] NATS subscribe (alias) {alt}")
                            await _sub(alt, cb=cb)
                    except Exception:
                        pass

                    # legacy text bridge -> wrap into minimal envelope and publish to same tg.input subject
                    async def cb_legacy(msg):
                        try:
                            data = _json.loads(msg.data.decode("utf-8"))
                        except Exception:
                            data = {}
                        # transform into minimal io.input envelope compatible with downstream
                        try:
                            text = (data or {}).get("text") or ""
                            chat_id = str((data or {}).get("chat_id") or "")
                            tg_msg_id = (data or {}).get("tg_msg_id") or 0
                            env = {
                                "event_id": str(uuid.uuid4()).replace("-", ""),
                                "kind": "io.input",
                                "ts": datetime.utcnow().isoformat() + "Z",
                                "dedup_key": f"legacy:{chat_id}:{tg_msg_id}",
                                "payload": {
                                    "type": "text",
                                    "source": "telegram",
                                    "bot_id": "",
                                    "hub_id": hub_id,
                                    "chat_id": chat_id,
                                    "user_id": chat_id,
                                    "update_id": str(tg_msg_id),
                                    "payload": {"text": text, "meta": {"msg_id": tg_msg_id}},
                                },
                                "meta": {"hub_id": hub_id},
                            }
                        except Exception:
                            env = data
                        try:
                            self.ctx.bus.publish(Event(type=subj, payload=env, source="io.nats", ts=time.time()))
                        except Exception:
                            pass

                    from datetime import datetime

                    # Legacy classic path subscription only when explicitly enabled
                    try:
                        if os.getenv("HUB_LISTEN_LEGACY", "0") == "1":
                            await _sub(subj_legacy, cb=cb_legacy)
                            aliases_env = os.getenv("HUB_INPUT_ALIASES", "")
                            aliases: List[str] = [a.strip() for a in aliases_env.split(",") if a.strip()]
                            seen = set([hub_id])
                            for aid in aliases:
                                if aid in seen:
                                    continue
                                seen.add(aid)
                                alt_legacy = f"io.tg.in.{aid}.text"
                                if hub_nats_verbose or not hub_nats_quiet:
                                    print(f"[hub-io] NATS subscribe (alias legacy) {alt_legacy}")
                                await _sub(alt_legacy, cb=cb_legacy)
                    except Exception:
                        pass
                    # keep task alive
                    try:
                        last_watchdog_tick_at = time.monotonic()
                        while True:
                            await asyncio.sleep(1.0)
                            now = time.monotonic()
                            tick_gap = now - last_watchdog_tick_at
                            last_watchdog_tick_at = now
                            skip_rx_watchdog = tick_gap > 5.0
                            if skip_rx_watchdog:
                                # If the event loop was stalled (e.g. a long sync handler), don't treat lack of RX
                                # during that window as a dead connection; refresh the baseline instead.
                                try:
                                    tr = getattr(nc, "_transport", None)
                                    if tr is not None:
                                        setattr(tr, "_adaos_last_rx_at", now)
                                except Exception:
                                    pass
                            # Watchdog: nats-py can silently lose its internal loops on unexpected WS/control frames
                            # (or other exceptions), leaving the socket open but the client effectively dead.
                            # If any core task terminates unexpectedly, restart the bridge.
                            try:
                                for _tname in ("_reading_task", "_flusher_task", "_ping_interval_task"):
                                    _t = getattr(nc, _tname, None)
                                    if isinstance(_t, asyncio.Task) and _t.done():
                                        _exc = None
                                        try:
                                            _exc = _t.exception()
                                        except asyncio.CancelledError:
                                            _exc = None
                                        # If the core task stopped without an exception, surface the last_error
                                        # so the supervisor can classify transient EOFs and quarantine the server.
                                        try:
                                            if _exc is None:
                                                _le = getattr(nc, "last_error", None)
                                                if isinstance(_le, Exception):
                                                    _exc = _le
                                        except Exception:
                                            pass
                                        # If task ended without exception, still restart - it should live forever.
                                        _msg = (
                                            f"[hub-io] nats watchdog: task={_tname} terminated exc={type(_exc).__name__}: {_exc}"
                                            if _exc
                                            else f"[hub-io] nats watchdog: task={_tname} terminated"
                                        )
                                        _rl_log("nats.watchdog", _msg, every_s=1.0)
                                        if isinstance(_exc, Exception):
                                            raise _exc
                                        raise RuntimeError(_msg)
                            except RuntimeError:
                                raise
                            except Exception:
                                pass
                            # RX watchdog: if we stop receiving WS frames (including keepalives) for too long,
                            # treat the connection as dead even if `nc.is_closed()` is still False.
                            try:
                                if skip_rx_watchdog:
                                    raise StopIteration()
                                tr = getattr(nc, "_transport", None)
                                last_rx = getattr(tr, "_adaos_last_rx_at", None) if tr is not None else None
                                if isinstance(last_rx, (int, float)):
                                    try:
                                        rx_timeout_s = float(os.getenv("HUB_NATS_RX_TIMEOUT_S", "90") or "90")
                                    except Exception:
                                        rx_timeout_s = 90.0
                                    if rx_timeout_s >= 10.0 and (time.monotonic() - float(last_rx)) > rx_timeout_s:
                                        _idle = time.monotonic() - float(last_rx)
                                        _msg = f"[hub-io] nats watchdog: no RX for {_idle:.1f}s (timeout={rx_timeout_s:.1f}s)"
                                        _rl_log("nats.watchdog", _msg, every_s=1.0)
                                        raise RuntimeError(_msg)
                            except StopIteration:
                                pass
                            except RuntimeError:
                                raise
                            except Exception:
                                pass

                            is_closed_attr = getattr(nc, "is_closed", None)
                            is_closed = is_closed_attr() if callable(is_closed_attr) else bool(is_closed_attr)
                            if is_closed:
                                # Extra WS diagnostics (close code/reason) for debugging UnexpectedEOF.
                                try:
                                    if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                        tr = getattr(nc, "_transport", None)
                                        ws = getattr(tr, "_ws", None) if tr else None
                                        ws_closed = getattr(ws, "closed", None) if ws is not None else None
                                        ws_close_code = getattr(ws, "close_code", None) if ws is not None else None
                                        ws_exc = None
                                        try:
                                            exf = getattr(ws, "exception", None)
                                            if callable(exf):
                                                ws_exc = exf()
                                        except Exception:
                                            ws_exc = None
                                        _rl_log(
                                            "nats.ws_state",
                                            f"[hub-io] nats ws state: tag={getattr(tr, '_adaos_ws_tag', None) if tr is not None else None} server={nats_last_server} ws_url={getattr(tr, '_adaos_ws_url', None) if tr is not None else None} closed={ws_closed} close_code={ws_close_code} ws_exc={ws_exc}",
                                            every_s=1.0,
                                        )
                                except Exception:
                                    pass
                                last_err = getattr(nc, "last_error", None)
                                details = f"{type(last_err).__name__}: {last_err}" if last_err else ""
                                raise RuntimeError(f"nats connection closed{(': ' + details) if details else ''}")
                    finally:
                        try:
                            if raw_keepalive_task is not None:
                                try:
                                    raw_keepalive_task.cancel()
                                except Exception:
                                    pass
                                try:
                                    await asyncio.wait_for(asyncio.gather(raw_keepalive_task, return_exceptions=True), timeout=1.0)
                                except Exception:
                                    pass
                                raw_keepalive_task = None
                        except Exception:
                            pass
                        try:
                            if getattr(self, "_tg_output_nats_nc", None) is nc:
                                setattr(self, "_tg_output_nats_nc", None)
                        except Exception:
                            pass
                        async def _force_close_ws_transport() -> None:
                            # nats-py WebSocketTransport can leave aiohttp.ClientSession unclosed
                            # if the websocket is already None (close() becomes a no-op and wait_closed() hangs).
                            try:
                                tr = getattr(nc, "_transport", None)
                                if not tr:
                                    return

                                ws = getattr(tr, "_ws", None)
                                close_task = getattr(tr, "_close_task", None)
                                client = getattr(tr, "_client", None)

                                try:
                                    if ws is not None:
                                        await ws.close()
                                except Exception:
                                    pass

                                # Unblock wait_closed() if it would otherwise await an unresolved Future.
                                try:
                                    if close_task is not None and hasattr(close_task, "done") and not close_task.done():
                                        close_task.set_result(None)
                                except Exception:
                                    pass

                                try:
                                    if client is not None:
                                        await client.close()
                                except Exception:
                                    pass

                                try:
                                    setattr(tr, "_ws", None)
                                    setattr(tr, "_client", None)
                                except Exception:
                                    pass
                            except Exception:
                                pass

                        # On shutdown/cancel, close any live proxy tunnels and unsubscribe.
                        try:
                            for k, rec in list(tunnels.items()):
                                try:
                                    ws = rec.get("ws") if isinstance(rec, dict) else None
                                    if ws:
                                        await ws.close()
                                except Exception:
                                    pass
                                tunnels.pop(k, None)
                        except Exception:
                            pass
                        try:
                            for k, tsk in list(tunnel_tasks.items()):
                                try:
                                    tsk.cancel()
                                except Exception:
                                    pass
                                tunnel_tasks.pop(k, None)
                        except Exception:
                            pass
                        try:
                            # Unsubscribe all subscriptions explicitly to ensure nats-py cancels
                            # internal subscription tasks before the next reconnect attempt.
                            for sub in list(subs):
                                try:
                                    unsub = sub.unsubscribe()
                                    if asyncio.iscoroutine(unsub):
                                        await unsub
                                except Exception:
                                    pass

                            # Ensure internal subscription tasks are stopped even if the connection is already closed.
                            for sub in list(subs):
                                try:
                                    stop = getattr(sub, "_stop_processing", None)
                                    if callable(stop):
                                        stop()
                                except Exception:
                                    pass

                            # Await/cancel internal subscription tasks, if present.
                            wait_tasks: list[asyncio.Task] = []
                            for sub in list(subs):
                                t = getattr(sub, "_wait_for_msgs_task", None)
                                if isinstance(t, asyncio.Task) and not t.done():
                                    try:
                                        t.cancel()
                                    except Exception:
                                        pass
                                    wait_tasks.append(t)
                            if wait_tasks:
                                try:
                                    await asyncio.wait_for(asyncio.gather(*wait_tasks, return_exceptions=True), timeout=1.0)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        try:
                            await asyncio.wait_for(nc.drain(), timeout=2.0)
                        except Exception:
                            pass
                        try:
                            await asyncio.wait_for(nc.close(), timeout=2.0)
                        except Exception:
                            pass
                        await _force_close_ws_transport()
                        # Give canceled subscription tasks a chance to finish to avoid
                        # "Task was destroyed but it is pending!" warnings.
                        try:
                            await asyncio.sleep(0)
                        except Exception:
                            pass

                async def _maybe_snapshot_root_logs(*, trace: bool, force: bool = False) -> None:
                    try:
                        if os.getenv("HUB_ROOT_LOG_SNAPSHOT", "0") != "1":
                            return
                        now = time.monotonic()
                        try:
                            snap_every_s = float(os.getenv("HUB_ROOT_LOG_SNAPSHOT_EVERY_S", "60") or "60")
                        except Exception:
                            snap_every_s = 60.0
                        if snap_every_s < 5.0:
                            snap_every_s = 5.0

                        nonlocal last_root_snapshot_at
                        if (not force) and last_root_snapshot_at is not None and (now - last_root_snapshot_at) < snap_every_s:
                            return
                        last_root_snapshot_at = now

                        base = None
                        try:
                            from urllib.parse import urlparse as _urlparse

                            u = _urlparse(str(nats_last_server or ""))
                            host = (u.hostname or "").strip()
                            if host:
                                # Dev endpoints (like /v1/dev/log_tail) live on the API host, not the NATS host.
                                # If we connected to `nats.<domain>`, try `api.<domain>` for snapshots.
                                if host.startswith("nats.") and host.count(".") >= 2:
                                    host = "api." + host.split(".", 1)[1]
                                base = ("https://" if str(u.scheme).startswith("wss") else "http://") + host
                        except Exception:
                            base = None
                        if not base:
                            return

                        files = os.getenv("HUB_ROOT_LOG_SNAPSHOT_FILES", "reverse-proxy.log,nats.log,backend-b.log") or ""
                        want = [x.strip() for x in files.split(",") if x.strip()]
                        if not want:
                            return
                        try:
                            lines = int(os.getenv("HUB_ROOT_LOG_SNAPSHOT_LINES", "250") or "250")
                        except Exception:
                            lines = 250
                        if lines < 50:
                            lines = 50

                        out_dir = Path(".adaos") / "root_log_snapshots"
                        out_dir.mkdir(parents=True, exist_ok=True)

                        def _fetch_one(fname: str) -> tuple[str, str]:
                            import urllib.parse as _up
                            import urllib.request as _ureq

                            qs = _up.urlencode({"file": fname, "lines": str(lines)})
                            url = f"{base}/v1/dev/log_tail?{qs}"
                            hdrs = {}
                            try:
                                # Root dev endpoints are protected by X-Root-Token.
                                tok = (os.getenv("HUB_ROOT_LOG_SNAPSHOT_ROOT_TOKEN", "") or "").strip()
                                if not tok:
                                    tok = (os.getenv("ROOT_TOKEN", "") or "").strip()
                                if not tok:
                                    tok = (os.getenv("ADAOS_ROOT_OWNER_TOKEN", "") or "").strip()
                                if not tok:
                                    # Back-compat: previously this env existed and users sometimes set
                                    # `Bearer <token>`; accept and normalize it.
                                    tok = (os.getenv("HUB_ROOT_LOG_SNAPSHOT_AUTH", "") or "").strip()
                                if tok.lower().startswith("bearer "):
                                    tok = tok.split(" ", 1)[1].strip()
                                if tok:
                                    hdrs["X-Root-Token"] = tok
                            except Exception:
                                pass
                            req = _ureq.Request(url, headers=hdrs)
                            with _ureq.urlopen(req, timeout=10) as resp:
                                body = resp.read().decode("utf-8", errors="replace")
                            return url, body

                        def _extract_tag_lines(body: str, tag: str) -> str:
                            try:
                                if not tag:
                                    return ""
                                import json as _json

                                obj = _json.loads(body)
                                lines0 = obj.get("lines", [])
                                if not isinstance(lines0, list):
                                    return ""
                                hits = [str(s) for s in lines0 if isinstance(s, str) and tag in s]
                                # Keep this file small and focused.
                                return "\n".join(hits[-500:])
                            except Exception:
                                return ""

                        nonlocal ws_connect_tag
                        tag0 = ws_connect_tag if isinstance(ws_connect_tag, str) else ""
                        ts = time.strftime("%Y%m%d_%H%M%SZ", time.gmtime())
                        for fname in want:
                            try:
                                url, body = await asyncio.to_thread(_fetch_one, fname)
                                fn = out_dir / f"{ts}__{(tag0 or 'no_tag')}__{fname.replace('/', '_')}"
                                fn.write_text(body, encoding="utf-8", errors="replace")
                                try:
                                    ex = _extract_tag_lines(body, tag0)
                                    if ex:
                                        fn2 = out_dir / f"{ts}__{(tag0 or 'no_tag')}__{fname.replace('/', '_')}__extract.log"
                                        fn2.write_text(ex, encoding="utf-8", errors="replace")
                                except Exception:
                                    pass
                                if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                    _rl_log("root.snap", f"[hub-io] saved root log snapshot {fn} (from {url})", every_s=1.0)
                            except Exception as _se:
                                if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                    _rl_log("root.snap_fail", f"[hub-io] root log snapshot failed file={fname} err={type(_se).__name__}: {_se}", every_s=1.0)
                    except Exception:
                        return

                # Supervisor wrapper: never crash on unhandled errors; restart with backoff
                async def _nats_bridge_supervisor() -> None:
                    delay = 1.0
                    while True:
                        started_at = time.monotonic()
                        trace0 = os.getenv("HUB_NATS_TRACE", "0") == "1"
                        try:
                            _rl_log("nats.supervisor.start", "[hub-io] nats supervisor: start bridge", every_s=5.0)
                            await _nats_bridge()
                            await asyncio.sleep(3600)
                        except asyncio.CancelledError:
                            return
                        except Exception as e:
                            try:
                                print(f"[hub-io] nats: encountered error: {e}")
                            except Exception:
                                pass
                            try:
                                await _maybe_snapshot_root_logs(trace=trace0, force=True)
                            except Exception:
                                pass
                            # Optional delayed snapshot: root-side logs (ECONNRESET/conn close) can be emitted
                            # slightly after the hub notices EOF. A second tail a few seconds later often captures it.
                            try:
                                # Always include an immediate post-error snapshot (0s) unless the user explicitly
                                # disables delayed snapshots by setting this to an empty string.
                                after_env = os.getenv("HUB_ROOT_LOG_SNAPSHOT_AFTER_ERR_S", "0,3") or "0,3"
                            except Exception:
                                after_env = "0,3"
                            delays: list[float] = []
                            try:
                                for part in str(after_env).split(","):
                                    p = str(part).strip()
                                    if not p:
                                        continue
                                    try:
                                        v = float(p)
                                    except Exception:
                                        continue
                                    if v >= 0:
                                        delays.append(v)
                            except Exception:
                                delays = []
                            if delays:
                                # Keep this bounded so the supervisor can restart promptly.
                                for after_s in delays[:8]:
                                    try:
                                        s = float(after_s)
                                        if s > 0:
                                            await asyncio.sleep(min(30.0, max(0.1, s)))
                                    except Exception:
                                        pass
                                    try:
                                        await _maybe_snapshot_root_logs(trace=trace0, force=True)
                                    except Exception:
                                        pass

                            ran_for_s = time.monotonic() - started_at
                            try:
                                low = str(e).lower()
                                is_transient = (
                                    type(e).__name__ in ("UnexpectedEOF", "ClientConnectionResetError")
                                    or "unexpected eof" in low
                                    or "connection reset" in low
                                    or "clientconnectionreseterror" in low
                                    or "cannot write to closing transport" in low
                                )
                            except Exception:
                                is_transient = False

                            try:
                                q_min_uptime_s = float(os.getenv("HUB_NATS_QUARANTINE_MIN_UPTIME_S", "90") or "90")
                            except Exception:
                                q_min_uptime_s = 90.0
                            try:
                                q_for_s = float(os.getenv("HUB_NATS_QUARANTINE_S", "300") or "300")
                            except Exception:
                                q_for_s = 300.0
                            try:
                                if is_transient and ran_for_s < q_min_uptime_s and isinstance(nats_last_server, str) and nats_last_server:
                                    q_seconds = max(30.0, q_for_s)
                                    nats_server_quarantine_until[nats_last_server] = time.monotonic() + q_seconds
                                    _rl_log(
                                        "nats.supervisor.quarantine",
                                        f"[hub-io] nats supervisor: quarantine server={nats_last_server} for {q_seconds:.0f}s (ran_for={ran_for_s:.1f}s)",
                                        every_s=1.0,
                                    )
                            except Exception:
                                pass

                            if ran_for_s >= 10.0 or is_transient:
                                delay = 0.5
                            try:
                                ok_ago = None
                                if nats_last_ok_at is not None:
                                    ok_ago = time.monotonic() - nats_last_ok_at
                                _rl_log(
                                    "nats.supervisor.retry",
                                    f"[hub-io] nats supervisor: retry in {delay:.1f}s (ran_for={ran_for_s:.1f}s ok_ago={ok_ago:.1f}s transient={is_transient})",
                                    every_s=1.0,
                                )
                            except Exception:
                                pass
                            await asyncio.sleep(delay)
                            if ran_for_s < 10.0 and not is_transient:
                                delay = min(delay * 2.0, 30.0)
                            else:
                                delay = min(max(delay, 0.5), 2.0)

                # TODO restore nats WS subscription
                self._boot_tasks.append(asyncio.create_task(_nats_bridge_supervisor(), name="adaos-nats-io-bridge"))
        except Exception:
            try:
                if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("ADAOS_CLI_DEBUG", "0") == "1":
                    print("[hub-io] nats init failed")
                    try:
                        tb = "".join(traceback.format_exception(*__import__("sys").exc_info()))
                        print(tb.rstrip())
                    except Exception:
                        pass
            except Exception:
                pass

    async def shutdown(self) -> None:
        await bus.emit("sys.stopping", {}, source="lifecycle", actor="system")
        try:
            await get_service_supervisor().shutdown()
        except Exception:
            pass
        for t in list(self._boot_tasks):
            try:
                t.cancel()
            except Exception:
                pass
        if self._boot_tasks:
            await asyncio.gather(*self._boot_tasks, return_exceptions=True)
            self._boot_tasks.clear()
        self._booted = False
        self._ready.clear()
        await bus.emit("sys.stopped", {}, source="lifecycle", actor="system")

    async def switch_role(self, app: Any, role: str, *, hub_url: str | None = None, subnet_id: str | None = None) -> NodeConfig:
        prev = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
        await self.shutdown()
        if prev.role == "member" and role.lower().strip() == "hub" and prev.hub_url:
            try:
                await self.heartbeat.deregister(prev.hub_url, prev.token or "", node_id=prev.node_id)
            except Exception:
                pass
            subnet_id = subnet_id or str(uuid.uuid4())
        conf = cfg_set_role(role, hub_url=hub_url, subnet_id=subnet_id, ctx=self.ctx)
        await self.run_boot_sequence(app or self._app)
        return conf


# --- модульные фасады (синглтон) ---
from adaos.services.heartbeat_requests import RequestsHeartbeat
from adaos.services.skills_loader_importlib import ImportlibSkillsLoader
from adaos.services.subnet_registry_mem import get_subnet_registry

_SERVICE: BootstrapService | None = None


def _svc() -> BootstrapService:
    global _SERVICE
    if _SERVICE is None:
        ctx = get_ctx()
        _SERVICE = BootstrapService(ctx, heartbeat=RequestsHeartbeat(), skills_loader=ImportlibSkillsLoader(), subnet_registry=get_subnet_registry())
    return _SERVICE


def is_ready() -> bool:
    return _svc().is_ready()


async def run_boot_sequence(app: Any) -> None:
    await _svc().run_boot_sequence(app)


async def shutdown() -> None:
    await _svc().shutdown()


async def switch_role(app: Any, role: str, *, hub_url: str | None = None, subnet_id: str | None = None) -> NodeConfig:
    return await _svc().switch_role(app, role, hub_url=hub_url, subnet_id=subnet_id)
