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
from adaos.services.nats_config import normalize_nats_ws_url, order_nats_ws_candidates
from adaos.services.realtime_sidecar import (
    realtime_sidecar_diag_path,
    realtime_sidecar_enabled,
    realtime_sidecar_log_path,
    realtime_sidecar_local_url,
    resolve_realtime_remote_candidates,
)
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
        hub_url = str(conf.hub_url or "").strip()
        if not hub_url:
            await bus.emit("net.subnet.register.error", {"status": "hub_url_missing"}, source="lifecycle", actor="system")
            return None
        try:
            ok = await self.heartbeat.register(
                hub_url,
                conf.token or "",
                node_id=conf.node_id,
                subnet_id=conf.subnet_id,
                hostname=socket.gethostname(),
                roles=["member"],
            )
        except Exception as exc:
            await bus.emit("net.subnet.register.error", {"error": str(exc)}, source="lifecycle", actor="system")
            return None
        if not ok:
            await bus.emit("net.subnet.register.error", {"status": "non-200"}, source="lifecycle", actor="system")
            return None
        await bus.emit("net.subnet.registered", {"hub": conf.hub_url}, source="lifecycle", actor="system")

        async def loop() -> None:
            backoff = 1
            while True:
                try:
                    ok_hb = await self.heartbeat.heartbeat(hub_url, conf.token or "", node_id=conf.node_id)
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
        # Unified deep-trace switch for WS/NATS/route debugging.
        try:
            if os.getenv("HUB_TRACE", "0") == "1":
                for k in (
                    "HUB_NATS_TRACE",
                    "HUB_NATS_VERBOSE",
                    "HUB_NATS_WS_TRACE",
                    "HUB_NATS_WIRETAP",
                    "HUB_NATS_WS_PATCH_AIOHTTP",
                    "HUB_ROUTE_TRACE",
                    "HUB_ROUTE_FRAME_VERBOSE",
                    "HUB_ROUTE_TX_VERBOSE",
                    "HUB_ROUTE_DIAG",
                    "HUB_WS_TRACE",
                    "HUB_ROOT_LOG_SNAPSHOT",
                    "HUB_ROOT_LOG_SNAPSHOT_EXTRACT_PRINT",
                ):
                    os.environ.setdefault(k, "1")
                os.environ.setdefault("HUB_NATS_WIRETAP_MAX_BYTES", "200")
                os.environ.setdefault("HUB_ROOT_LOG_SNAPSHOT_LINES", "2000")
                os.environ.setdefault("ADAOS_LOOP_LAG_MONITOR", "1")
                try:
                    print("[hub-io] HUB_TRACE=1 -> enabling deep WS/NATS/route tracing")
                except Exception:
                    pass
        except Exception:
            pass
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
            hub_id = load_config(ctx=self.ctx).subnet_id
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
                        from adaos.services.capacity import _load_node_yaml as _load_node, _save_node_yaml as _save_node

                        nd = _load_node()
                        node_nats = (nd or {}).get("nats") if isinstance(nd, dict) else None
                        if not isinstance(node_nats, dict) or not node_nats:
                            return None, None, None
                        raw_nurl = str(node_nats.get("ws_url") or "").strip() or None
                        nurl = normalize_nats_ws_url(raw_nurl, fallback=None)
                        nuser = str(node_nats.get("user") or "") or None
                        npass = str(node_nats.get("pass") or "") or None
                        if nurl and raw_nurl and nurl != raw_nurl:
                            node_nats["ws_url"] = nurl
                            nd["nats"] = node_nats
                            _save_node(nd)
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
                    nats_ws_url = normalize_nats_ws_url(data.get("nats_ws_url"))
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
                last_ws_transport: str | None = None

                async def _nats_bridge() -> None:
                    nonlocal reported_down
                    nonlocal nats_last_ok_at
                    nonlocal nats_last_server
                    nonlocal last_ws_transport
                    backoff = 1.0
                    trace = os.getenv("HUB_NATS_TRACE", "0") == "1"
                    if trace or os.getenv("HUB_NATS_VERBOSE", "0") == "1":
                        try:
                            import asyncio as _asyncio

                            policy = _asyncio.get_event_loop_policy()
                            try:
                                loop = _asyncio.get_running_loop()
                            except RuntimeError:
                                loop = None
                            _rl_log(
                                "loop.info",
                                f"[hub-io] asyncio loop policy={type(policy).__name__} loop={type(loop).__name__ if loop else None}",
                                every_s=3600.0,
                            )
                            if loop is not None and os.name == "nt" and "Selector" in type(loop).__name__:
                                _rl_log(
                                    "loop.warn",
                                    "[hub-io] Windows Selector event loop detected; NATS-over-WS may stall on PUB load. Prefer default Proactor loop and only set ADAOS_WIN_SELECTOR_LOOP=1 for targeted diagnostics.",
                                    every_s=3600.0,
                                )
                        except Exception:
                            pass
                    raw_keepalive_task: asyncio.Task | None = None
                    try:
                        realtime_enabled = realtime_sidecar_enabled(
                            role=str(getattr(self.ctx.config, "role", "") or "").strip().lower()
                        )
                    except Exception:
                        realtime_enabled = False
                    # Best-effort outbox for telegram replies when NATS is flapping.
                    try:
                        if not hasattr(self, "_tg_output_pending"):
                            setattr(self, "_tg_output_pending", deque())
                    except Exception:
                        pass
                    if realtime_enabled:
                        last_ws_transport = "sidecar"
                        if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                            _rl_log(
                                "nats.ws_transport",
                                f"[hub-io] nats ws transport: sidecar (internal WS client disabled, local={realtime_sidecar_local_url()})",
                                every_s=3600.0,
                            )
                    else:
                        # NATS WS transport: use `websockets` (avoid aiohttp WS flaps under PUB load).
                        try:
                            from adaos.services.nats_ws_transport import install_nats_ws_transport_patch

                            ws_transport = install_nats_ws_transport_patch(verbose=False)
                            last_ws_transport = ws_transport
                            if (os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace) and ws_transport:
                                _rl_log(
                                    "nats.ws_transport",
                                    f"[hub-io] nats ws transport: {ws_transport}",
                                    every_s=3600.0,
                                )
                        except Exception as _patch_e:
                            if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                _rl_log(
                                    "nats.ws_transport_patch_err",
                                    f"[hub-io] nats ws transport patch error: {type(_patch_e).__name__}: {_patch_e}",
                                    every_s=5.0,
                                )

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
                                candidates = order_nats_ws_candidates(
                                    candidates,
                                    explicit_url=base,
                                    prefer_dedicated=pref_ded,
                                )
                            except Exception:
                                pass
                            try:
                                if realtime_enabled:
                                    remote_candidates = resolve_realtime_remote_candidates()
                                    candidates = [realtime_sidecar_local_url()]
                                    _rl_log(
                                        "nats.sidecar_route",
                                        f"[hub-io] nats realtime sidecar local={candidates[0]} remote={remote_candidates}",
                                        every_s=60.0,
                                    )
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
                                                    # Extract handshake info if present
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

                            diag_file_state: dict[str, float | None] = {"last_at": None}

                            def _nats_task_snapshot(task: Any, *, stack_limit: int = 6) -> dict[str, Any] | None:
                                if not isinstance(task, asyncio.Task):
                                    return None
                                snap: dict[str, Any] = {
                                    "done": bool(task.done()),
                                    "cancelled": bool(task.cancelled()),
                                }
                                try:
                                    exc = task.exception() if task.done() and not task.cancelled() else None
                                    snap["exc"] = f"{type(exc).__name__}: {exc}" if exc is not None else None
                                except Exception as exc:
                                    snap["exc"] = f"{type(exc).__name__}: {exc}"
                                frames: list[str] = []
                                try:
                                    for frame in task.get_stack(limit=max(1, int(stack_limit))):
                                        try:
                                            frames.append(
                                                f"{Path(frame.f_code.co_filename).name}:{int(frame.f_lineno)}:{frame.f_code.co_name}"
                                            )
                                        except Exception:
                                            continue
                                except Exception as exc:
                                    frames = [f"{type(exc).__name__}: {exc}"]
                                snap["stack"] = frames
                                return snap

                            def _write_nats_ws_diag_file(
                                nc_for_diag: Any,
                                *,
                                server: Any | None = None,
                                source: str | None = None,
                                task_name: str | None = None,
                                err: Exception | None = None,
                                force: bool = False,
                            ) -> None:
                                raw_path = str(os.getenv("HUB_NATS_WS_DIAG_FILE", "") or "").strip()
                                if not raw_path:
                                    return
                                try:
                                    every_s = float(os.getenv("HUB_NATS_WS_DIAG_EVERY_S", "2") or "2")
                                except Exception:
                                    every_s = 2.0
                                if every_s <= 0.0:
                                    every_s = 2.0
                                now_mono = time.monotonic()
                                last_at = diag_file_state.get("last_at")
                                if (
                                    not force
                                    and source == "periodic"
                                    and isinstance(last_at, (int, float))
                                    and (now_mono - float(last_at)) < max(0.5, every_s)
                                ):
                                    return
                                diag_file_state["last_at"] = now_mono
                                try:
                                    stack_limit = int(os.getenv("HUB_NATS_WS_DIAG_STACK_LIMIT", "6") or "6")
                                except Exception:
                                    stack_limit = 6
                                try:
                                    loop = asyncio.get_running_loop()
                                except RuntimeError:
                                    loop = None
                                try:
                                    policy = asyncio.get_event_loop_policy()
                                except Exception:
                                    policy = None
                                tr = getattr(nc_for_diag, "_transport", None)
                                ws = getattr(tr, "_ws", None) if tr is not None else None

                                def _ago(attr: str) -> float | None:
                                    try:
                                        value = getattr(tr, attr, None) if tr is not None else None
                                        if isinstance(value, (int, float)):
                                            return round(now_mono - float(value), 3)
                                    except Exception:
                                        return None
                                    return None

                                connected_attr = getattr(nc_for_diag, "is_connected", None)
                                closed_attr = getattr(nc_for_diag, "is_closed", None)
                                snapshot: dict[str, Any] = {
                                    "ts": round(time.time(), 3),
                                    "source": source,
                                    "task_name": task_name,
                                    "server": server if server is not None else nats_last_server,
                                    "loop_policy": type(policy).__name__ if policy is not None else None,
                                    "loop": type(loop).__name__ if loop is not None else None,
                                    "nc_connected": connected_attr() if callable(connected_attr) else bool(connected_attr),
                                    "nc_closed": closed_attr() if callable(closed_attr) else bool(closed_attr),
                                    "transport": type(tr).__name__ if tr is not None else None,
                                    "ws_url": getattr(tr, "_adaos_ws_url", None) if tr is not None else None,
                                    "ws_tag": getattr(tr, "_adaos_ws_tag", None) if tr is not None else None,
                                    "ws_proto": getattr(tr, "_adaos_ws_proto", None) if tr is not None else None,
                                    "ws_closed": getattr(ws, "closed", None) if ws is not None else None,
                                    "ws_close_code": getattr(ws, "close_code", None) if ws is not None else None,
                                    "last_rx_ago_s": _ago("_adaos_last_rx_at"),
                                    "last_tx_ago_s": _ago("_adaos_last_tx_at"),
                                    "last_ping_rx_ago_s": _ago("_adaos_last_ping_rx_at"),
                                    "last_pong_tx_ago_s": _ago("_adaos_last_pong_tx_at"),
                                    "last_ws_ping_tx_ago_s": _ago("_adaos_last_ws_ping_tx_at"),
                                    "ka_pings_rx": getattr(tr, "_adaos_pings_rx", None) if tr is not None else None,
                                    "ka_pongs_tx": getattr(tr, "_adaos_pongs_tx", None) if tr is not None else None,
                                    "ws_pings_tx": getattr(tr, "_adaos_ws_pings_tx", None) if tr is not None else None,
                                    "last_tx_kind": getattr(tr, "_adaos_last_tx_kind", None) if tr is not None else None,
                                    "last_tx_subj": getattr(tr, "_adaos_last_tx_subj", None) if tr is not None else None,
                                    "pending_data_size": getattr(nc_for_diag, "_pending_data_size", None),
                                    "reading_task": _nats_task_snapshot(getattr(nc_for_diag, "_reading_task", None), stack_limit=stack_limit),
                                    "flusher_task": _nats_task_snapshot(getattr(nc_for_diag, "_flusher_task", None), stack_limit=stack_limit),
                                    "ping_interval_task": _nats_task_snapshot(getattr(nc_for_diag, "_ping_interval_task", None), stack_limit=stack_limit),
                                    "err": f"{type(err).__name__}: {err}" if err is not None else None,
                                }
                                try:
                                    path = Path(raw_path)
                                    if not path.is_absolute():
                                        path = Path.cwd() / path
                                    path.parent.mkdir(parents=True, exist_ok=True)
                                    with path.open("a", encoding="utf-8") as fh:
                                        fh.write(_json.dumps(snapshot, ensure_ascii=False) + "\n")
                                except Exception:
                                    pass

                            def _log_nats_ws_diag(
                                nc_for_diag: Any,
                                *,
                                server: Any | None = None,
                                rate_key: str = "nats.ws_diag",
                                every_s: float = 1.0,
                                source: str | None = None,
                                task_name: str | None = None,
                                err: Exception | None = None,
                            ) -> tuple[Any, Any, Any, Any]:
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
                                    tr_pending_hi_q = None
                                    try:
                                        q_hi = getattr(tr, "_pending_hi", None) if tr is not None else None
                                        if q_hi is not None and callable(getattr(q_hi, "qsize", None)):
                                            tr_pending_hi_q = q_hi.qsize()
                                    except Exception:
                                        tr_pending_hi_q = None
                                    send_lock_locked = None
                                    try:
                                        lk = getattr(tr, "_send_lock", None) if tr is not None else None
                                        if lk is not None and callable(getattr(lk, "locked", None)):
                                            send_lock_locked = bool(lk.locked())
                                    except Exception:
                                        send_lock_locked = None
                                    ka_pings_rx = None
                                    ka_last_ping_rx_ago_s = None
                                    try:
                                        ka_pings_rx = getattr(tr, "_adaos_pings_rx", None) if tr is not None else None
                                        ka_last_ping_rx_at = getattr(tr, "_adaos_last_ping_rx_at", None) if tr is not None else None
                                        if isinstance(ka_last_ping_rx_at, (int, float)):
                                            ka_last_ping_rx_ago_s = round(time.monotonic() - float(ka_last_ping_rx_at), 3)
                                    except Exception:
                                        ka_pings_rx = ka_pings_rx or None
                                        ka_last_ping_rx_ago_s = ka_last_ping_rx_ago_s or None
                                    ka_pongs_tx = None
                                    ka_last_pong_tx_ago_s = None
                                    ka_last_pong_wait_ms = None
                                    ka_last_pong_send_ms = None
                                    try:
                                        ka_pongs_tx = getattr(tr, "_adaos_pongs_tx", None) if tr is not None else None
                                        ka_last_pong_tx_at = getattr(tr, "_adaos_last_pong_tx_at", None) if tr is not None else None
                                        if isinstance(ka_last_pong_tx_at, (int, float)):
                                            ka_last_pong_tx_ago_s = round(time.monotonic() - float(ka_last_pong_tx_at), 3)
                                        w_s = getattr(tr, "_adaos_last_pong_tx_wait_s", None) if tr is not None else None
                                        if isinstance(w_s, (int, float)):
                                            ka_last_pong_wait_ms = round(float(w_s) * 1000.0, 3)
                                        s_s = getattr(tr, "_adaos_last_pong_tx_send_s", None) if tr is not None else None
                                        if isinstance(s_s, (int, float)):
                                            ka_last_pong_send_ms = round(float(s_s) * 1000.0, 3)
                                    except Exception:
                                        ka_pongs_tx = ka_pongs_tx or None
                                        ka_last_pong_tx_ago_s = ka_last_pong_tx_ago_s or None
                                        ka_last_pong_wait_ms = ka_last_pong_wait_ms or None
                                        ka_last_pong_send_ms = ka_last_pong_send_ms or None
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
                                    ws_hb_mode = None
                                    try:
                                        ws_hb_mode = getattr(tr, "_adaos_ws_heartbeat_mode", None) if tr is not None else None
                                    except Exception:
                                        ws_hb_mode = None
                                    ws_data_hb = None
                                    try:
                                        ws_data_hb = getattr(tr, "_adaos_ws_data_heartbeat", None) if tr is not None else None
                                    except Exception:
                                        ws_data_hb = None
                                    ws_recv_timeout = None
                                    try:
                                        ws_recv_timeout = getattr(tr, "_adaos_ws_recv_timeout", None) if tr is not None else None
                                    except Exception:
                                        ws_recv_timeout = None
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
                                    last_recv_err = None
                                    last_recv_err_ago_s = None
                                    try:
                                        last_recv_err = getattr(tr, "_adaos_last_recv_error", None) if tr is not None else None
                                        last_recv_err_at = getattr(tr, "_adaos_last_recv_error_at", None) if tr is not None else None
                                        if isinstance(last_recv_err_at, (int, float)):
                                            last_recv_err_ago_s = round(time.monotonic() - float(last_recv_err_at), 3)
                                    except Exception:
                                        last_recv_err = last_recv_err or None
                                        last_recv_err_ago_s = last_recv_err_ago_s or None
                                    ws_pings_tx = None
                                    ws_last_ping_tx_ago_s = None
                                    ws_last_ping_wait_ms = None
                                    ws_last_ping_send_ms = None
                                    try:
                                        ws_pings_tx = getattr(tr, "_adaos_ws_pings_tx", None) if tr is not None else None
                                        ws_last_ping_tx_at = getattr(tr, "_adaos_last_ws_ping_tx_at", None) if tr is not None else None
                                        if isinstance(ws_last_ping_tx_at, (int, float)):
                                            ws_last_ping_tx_ago_s = round(time.monotonic() - float(ws_last_ping_tx_at), 3)
                                        ws_ping_wait_s = getattr(tr, "_adaos_last_ws_ping_tx_wait_s", None) if tr is not None else None
                                        if isinstance(ws_ping_wait_s, (int, float)):
                                            ws_last_ping_wait_ms = round(float(ws_ping_wait_s) * 1000.0, 3)
                                        ws_ping_send_s = getattr(tr, "_adaos_last_ws_ping_tx_send_s", None) if tr is not None else None
                                        if isinstance(ws_ping_send_s, (int, float)):
                                            ws_last_ping_send_ms = round(float(ws_ping_send_s) * 1000.0, 3)
                                    except Exception:
                                        ws_pings_tx = ws_pings_tx or None
                                        ws_last_ping_tx_ago_s = ws_last_ping_tx_ago_s or None
                                        ws_last_ping_wait_ms = ws_last_ping_wait_ms or None
                                        ws_last_ping_send_ms = ws_last_ping_send_ms or None
                                    server0 = server if server is not None else nats_last_server
                                    extra_parts: list[str] = []
                                    if source:
                                        extra_parts.append(f"source={source}")
                                    if task_name:
                                        extra_parts.append(f"task={task_name}")
                                    if err is not None:
                                        extra_parts.append(f"err={type(err).__name__}: {err}")
                                    extra_suffix = (" " + " ".join(extra_parts)) if extra_parts else ""
                                    _rl_log(
                                        rate_key,
                                        f"[hub-io] nats ws diag: tag={ws_tag} server={server0} ws_hb_s={ws_hb} ws_hb_mode={ws_hb_mode} ws_data_hb_s={ws_data_hb} ws_recv_timeout_s={ws_recv_timeout} ws_url={ws_url} closed={ws_closed} close_code={ws_close_code} close_reason={ws_close_reason} ws_exc={ws_exc} last_rx_ago_s={last_rx_ago_s} last_tx_ago_s={last_tx_ago_s} tx_connect_ago_s={tx_connect_ago_s} rx_info_ago_s={rx_info_ago_s} max_payload={max_payload} pending_data_size={pending_data_size} pings_outstanding={pings_outstanding} pongs_q={pongs_q} transport_pending_hi_q={tr_pending_hi_q} transport_pending_q={tr_pending_q} send_lock={send_lock_locked} ka_pings_rx={ka_pings_rx} ka_last_ping_rx_ago_s={ka_last_ping_rx_ago_s} ka_pongs_tx={ka_pongs_tx} ka_last_pong_tx_ago_s={ka_last_pong_tx_ago_s} ka_last_pong_wait_ms={ka_last_pong_wait_ms} ka_last_pong_send_ms={ka_last_pong_send_ms} ws_pings_tx={ws_pings_tx} ws_last_ping_tx_ago_s={ws_last_ping_tx_ago_s} ws_last_ping_wait_ms={ws_last_ping_wait_ms} ws_last_ping_send_ms={ws_last_ping_send_ms} ws_proto={ws_proto} last_tx_kind={last_tx_kind} last_tx_subj={last_tx_subj} last_tx_len={last_tx_len} last_recv_err={type(last_recv_err).__name__ if last_recv_err is not None else None} last_recv_err_ago_s={last_recv_err_ago_s}{extra_suffix}",
                                        every_s=every_s,
                                    )
                                except Exception:
                                    pass
                                return ws_closed, ws_close_code, ws_close_reason, ws_exc

                            async def _on_error_cb(e: Exception, *, nc_for_diag: Any | None = None) -> None:
                                # Best-effort; keep quiet unless explicitly verbose or useful
                                is_eof = type(e).__name__ == "UnexpectedEOF" or "unexpected eof" in str(e).lower()
                                if os.getenv("SILENCE_NATS_EOF", "0") == "1" and is_eof:
                                    return
                                # Emit extra transport diagnostics to correlate client-side errors with root-side logs.
                                if nc_for_diag is not None and (is_eof or os.getenv("HUB_NATS_VERBOSE", "0") == "1" or os.getenv("HUB_NATS_TRACE", "0") == "1"):
                                    try:
                                        ws_closed, ws_close_code, ws_close_reason, ws_exc = _log_nats_ws_diag(
                                            nc_for_diag,
                                            server=nats_last_server,
                                            rate_key="nats.ws_diag",
                                            every_s=1.0,
                                            source="error_cb",
                                            err=e,
                                        )
                                        _rl_log(
                                            "nats.ws_eof",
                                            f"[hub-io] nats ws eof: closed={ws_closed} close_code={ws_close_code} close_reason={ws_close_reason} ws_exc={ws_exc}",
                                            every_s=1.0,
                                        )
                                        _write_nats_ws_diag_file(
                                            nc_for_diag,
                                            server=nats_last_server,
                                            source="error_cb",
                                            err=e,
                                            force=True,
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
                                            "HUB_NATS_WS_MAX_MSG_SIZE",
                                            "HUB_NATS_WS_MAX_QUEUE",
                                            "HUB_NATS_WS_HEARTBEAT_S",
                                            "HUB_NATS_WS_DATA_HEARTBEAT_S",
                                            "HUB_NATS_WS_PROXY",
                                            "HUB_NATS_WS_TRACE",
                                            "HUB_NATS_WS_PATCH_AIOHTTP",
                                            "HUB_NATS_WIRETAP",
                                            "HUB_NATS_WIRETAP_MAX_BYTES",
                                            "HUB_NATS_WIRETAP_EVERY_N",
                                            "HUB_NATS_WIRETAP_SKIP",
                                            "HUB_NATS_TCP_KEEPALIVE",
                                            "HUB_NATS_TCP_KEEPALIVE_S",
                                            "HUB_NATS_TCP_KEEPALIVE_INTERVAL_S",
                                            "HUB_NATS_TCP_KEEPALIVE_PROBES",
                                            "HUB_NATS_RAW_KEEPALIVE",
                                            "HUB_NATS_RAW_KEEPALIVE_S",
                                            "HUB_NATS_CONNECT_TAG_QUERY",
                                            "HUB_TRACE",
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
                                    if type(e).__name__ == "SlowConsumerError":
                                        try:
                                            sub_sc = getattr(e, "sub", None)
                                            q_sc = getattr(sub_sc, "_pending_queue", None) if sub_sc is not None else None
                                            qsize_sc = q_sc.qsize() if q_sc is not None and callable(getattr(q_sc, "qsize", None)) else None
                                        except Exception:
                                            qsize_sc = None
                                        try:
                                            pending_size_sc = getattr(sub_sc, "_pending_size", None) if sub_sc is not None else None
                                        except Exception:
                                            pending_size_sc = None
                                        try:
                                            subject_sc = getattr(e, "subject", None)
                                        except Exception:
                                            subject_sc = None
                                        try:
                                            sid_sc = getattr(e, "sid", None)
                                        except Exception:
                                            sid_sc = None
                                        try:
                                            self._log.warning(
                                                "nats slow consumer hub_id=%s server=%s subject=%s sid=%s qsize=%s pending_size=%s",
                                                hub_id,
                                                nats_last_server,
                                                subject_sc,
                                                sid_sc,
                                                qsize_sc,
                                                pending_size_sc,
                                            )
                                        except Exception:
                                            pass
                                        try:
                                            _rl_log(
                                                "nats.slow_consumer",
                                                f"[hub-io] nats slow consumer subject={subject_sc} sid={sid_sc} qsize={qsize_sc} pending_size={pending_size_sc}",
                                                every_s=1.0,
                                            )
                                        except Exception:
                                            pass
                                    self._log.warning(
                                        "nats error_cb hub_id=%s server=%s type=%s err=%s",
                                        hub_id,
                                        nats_last_server,
                                        type(e).__name__,
                                        str(e),
                                    )
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
                                try:
                                    self._log.warning("nats disconnected hub_id=%s server=%s", hub_id, nats_last_server)
                                except Exception:
                                    pass
                                _emit_down("disconnected", None)

                            async def _on_reconnected() -> None:
                                try:
                                    self._log.info("nats reconnected hub_id=%s server=%s", hub_id, nats_last_server)
                                except Exception:
                                    pass
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
                            if is_ws_candidates or realtime_enabled:
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
                                        if (
                                            connect_server.startswith("ws")
                                            and os.getenv("HUB_NATS_CONNECT_TAG_QUERY", "0") == "1"
                                            and isinstance(ws_connect_tag, str)
                                            and ws_connect_tag
                                        ):
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
                                            ws_connection_headers=(
                                                {"X-AdaOS-Nats-Conn": [ws_connect_tag]}
                                                if connect_server.startswith("ws") and isinstance(ws_connect_tag, str) and ws_connect_tag
                                                else None
                                            ),
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
                                        tr = getattr(nc_local, "_transport", None)
                                        if tr is not None:
                                            try:
                                                setattr(tr, "_adaos_nc", nc_local)
                                            except Exception:
                                                pass
                                        if os.getenv("HUB_NATS_VERBOSE", "0") == "1" or trace:
                                            hb = getattr(tr, "_adaos_ws_heartbeat", None) if tr else None
                                            hb_mode = getattr(tr, "_adaos_ws_heartbeat_mode", None) if tr else None
                                            if hb is not None:
                                                _rl_log(
                                                    "nats.ws_hb",
                                                    f"[hub-io] nats ws heartbeat: {hb!s}s mode={hb_mode}",
                                                    every_s=60.0,
                                                )
                                            if isinstance(ws_connect_tag, str) and ws_connect_tag:
                                                _rl_log("nats.ws_tag", f"[hub-io] nats ws tag: {ws_connect_tag}", every_s=1.0)
                                            _rl_log(
                                                "nats.transport",
                                                f"[hub-io] nats transport kind: {type(tr).__name__ if tr is not None else None}",
                                                every_s=60.0,
                                            )
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
                            sub_workers: list[asyncio.Task] = []
                            _route_dispatch_trace = (
                                os.getenv("HUB_ROUTE_DISPATCH_TRACE", "0") == "1"
                                or os.getenv("HUB_ROUTE_TRACE", "0") == "1"
                                or os.getenv("HUB_TRACE", "0") == "1"
                            )

                            def _route_dispatch_log(msg0: str) -> None:
                                if not _route_dispatch_trace:
                                    return
                                try:
                                    print(msg0)
                                except Exception:
                                    pass

                            def _sub_qsize(sub0: Any) -> int | None:
                                try:
                                    q0 = getattr(sub0, "_pending_queue", None)
                                    if q0 is None:
                                        return None
                                    qsize = getattr(q0, "qsize", None)
                                    if callable(qsize):
                                        return int(qsize())
                                except Exception:
                                    return None
                                return None

                            async def _sub(subject: str, *, cb: Any):
                                sub = await nc.subscribe(subject)
                                # `nats-py` queues SUB locally and may not push it to the server until
                                # some later flush / publish. That breaks Root->Hub routing because
                                # `route.to_hub.*` must be active before the first proxied request arrives.
                                fp = getattr(nc, "_flush_pending", None)
                                if callable(fp):
                                    try:
                                        await asyncio.wait_for(fp(force_flush=True), timeout=2.0)
                                    except TypeError:
                                        try:
                                            await asyncio.wait_for(fp(True), timeout=2.0)
                                        except TypeError:
                                            await asyncio.wait_for(fp(), timeout=2.0)
                                else:
                                    await nc.flush(timeout=2.0)
                                subs.append(sub)

                                async def _runner() -> None:
                                    try:
                                        async for msg in sub.messages:
                                            try:
                                                msg_subject = ""
                                                msg_bytes = None
                                                started = None
                                                if _route_dispatch_trace and (
                                                    subject == "route.to_hub.*"
                                                    or subject.startswith("route.to_hub.")
                                                    or subject.startswith("route.to_browser.")
                                                ):
                                                    try:
                                                        msg_subject = str(getattr(msg, "subject", "") or "")
                                                    except Exception:
                                                        msg_subject = ""
                                                    try:
                                                        raw0 = bytes(getattr(msg, "data", b"") or b"")
                                                        msg_bytes = len(raw0)
                                                    except Exception:
                                                        msg_bytes = None
                                                    started = time.monotonic()
                                                    _route_dispatch_log(
                                                        f"[hub-route:dispatch] start sub={subject} msg={msg_subject} qsize={_sub_qsize(sub)} bytes={msg_bytes}"
                                                    )
                                                await cb(msg)
                                                if started is not None:
                                                    took_ms = (time.monotonic() - started) * 1000.0
                                                    _route_dispatch_log(
                                                        f"[hub-route:dispatch] done sub={subject} msg={msg_subject} qsize={_sub_qsize(sub)} took_ms={took_ms:.1f}"
                                                    )
                                            except asyncio.CancelledError:
                                                raise
                                            except Exception as e:
                                                try:
                                                    self._log.warning(
                                                        "nats subscription handler failed subject=%s type=%s err=%s",
                                                        subject,
                                                        type(e).__name__,
                                                        e,
                                                    )
                                                except Exception:
                                                    pass
                                    except asyncio.CancelledError:
                                        return
                                    except Exception as e:
                                        try:
                                            self._log.warning(
                                                "nats subscription worker stopped subject=%s type=%s err=%s",
                                                subject,
                                                type(e).__name__,
                                                e,
                                            )
                                        except Exception:
                                            pass

                                task = asyncio.create_task(_runner(), name=f"adaos-nats-sub-{subject}")
                                sub_workers.append(task)
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
                            try:
                                self._log.info(
                                    "nats bridge connected server=%s hub_id=%s",
                                    connected_server or "unknown",
                                    hub_id,
                                )
                            except Exception:
                                pass
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
                        msg_subject = str(getattr(msg, "subject", "") or subj)
                        if trace:
                            try:
                                _rl_log("nats.msg", f"[hub-io] nats recv subject={msg_subject} bytes={len(getattr(msg, 'data', b'') or b'')}", every_s=0.2)
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
                            self.ctx.bus.publish(Event(type=msg_subject, payload=data, source="io.nats", ts=time.time()))
                        except Exception:
                            pass

                    await _sub(subj, cb=cb)
                    try:
                        self._log.info("nats bridge subscribed subject=%s", subj)
                    except Exception:
                        pass

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
                        pending_tunnel_events: dict[str, list[dict[str, Any]]] = {}
                        pending_tunnel_meta: dict[str, dict[str, Any]] = {}
                        MAX_CHUNK_RAW = 300_000
                        MAX_PENDING_TUNNEL_EVENTS = 128

                        _route_verbose = os.getenv("HUB_ROUTE_VERBOSE", "0") == "1"
                        _route_diag = _route_verbose or os.getenv("HUB_ROUTE_DIAG", "0") == "1"
                        # Tx logs are extremely noisy (one line per request / response). Keep them separately gated.
                        _route_tx_verbose = os.getenv("HUB_ROUTE_TX_VERBOSE", "0") == "1"
                        # Trace is an opt-in "everything we know" log for debugging WS routing breaks.
                        _route_trace = os.getenv("HUB_ROUTE_TRACE", "0") == "1"
                        _route_http_trace = (
                            _route_trace
                            or os.getenv("HUB_ROUTE_HTTP_TRACE", "0") == "1"
                            or os.getenv("HUB_TRACE", "0") == "1"
                        )
                        # Frame logs are extremely noisy; keep them explicitly gated.
                        _route_frame_verbose = (
                            os.getenv("HUB_ROUTE_FRAME_VERBOSE", "0") == "1"
                            or os.getenv("ROUTE_PROXY_FRAME_VERBOSE", "0") == "1"
                        )
                        try:
                            _route_no_upstream_close_after_s = float(
                                os.getenv("HUB_ROUTE_FORCE_CLOSE_NO_UPSTREAM_S", "0") or "0"
                            )
                        except Exception:
                            _route_no_upstream_close_after_s = 0.0

                        try:
                            route_run_id = uuid.uuid4().hex[:6]
                        except Exception:
                            route_run_id = "route"
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

                        def _route_log(msg: str) -> None:
                            if not (_route_trace or _route_verbose):
                                return
                            try:
                                print(f"[hub-route:{route_run_id}] {msg}")
                            except Exception:
                                pass

                        def _key_tag(key: str) -> str:
                            try:
                                if not isinstance(key, str):
                                    return "?"
                                return key[-8:] if len(key) > 12 else key
                            except Exception:
                                return "?"

                        def _route_payload_summary(payload: dict[str, Any] | None) -> str:
                            try:
                                p0 = payload or {}
                                t0 = str(p0.get("t") or "")
                                if t0 == "http":
                                    m0 = str(p0.get("method") or "GET").upper()
                                    pth0 = str(p0.get("path") or "")
                                    return f"t=http method={m0} path={pth0}"
                                if t0 == "http_resp":
                                    status0 = p0.get("status")
                                    err0 = p0.get("err")
                                    truncated0 = p0.get("truncated")
                                    body0 = p0.get("body_b64")
                                    body_len0 = len(body0) if isinstance(body0, str) else None
                                    return f"t=http_resp status={status0} truncated={truncated0} body_b64_len={body_len0} err={err0}"
                                if t0 == "open":
                                    pth0 = str(p0.get("path") or "")
                                    q0 = str(p0.get("query") or "")
                                    return (
                                        f"t=open path={pth0} query_len={len(q0)} "
                                        f"token={_query_has_token(q0)} dev={_query_param(q0, 'dev')} ws={_query_param(q0, 'ws')}"
                                    )
                                if t0 in ("frame", "chunk"):
                                    kind0 = p0.get("kind")
                                    size0 = None
                                    data0 = p0.get("data") or p0.get("data_b64")
                                    try:
                                        size0 = len(data0) if data0 is not None else None
                                    except Exception:
                                        size0 = None
                                    if t0 == "chunk":
                                        return (
                                            f"t=chunk kind={kind0} idx={p0.get('idx')} total={p0.get('total')} size={size0}"
                                        )
                                    return f"t=frame kind={kind0} size={size0}"
                                if t0 == "close":
                                    return f"t=close err={p0.get('err')}"
                                return f"t={t0}"
                            except Exception:
                                return "t=?"

                        def _query_has_token(query: str) -> bool:
                            if not isinstance(query, str) or not query:
                                return False
                            try:
                                from urllib.parse import parse_qs

                                raw = query[1:] if query.startswith("?") else query
                                q = parse_qs(raw, keep_blank_values=True)
                                return "token" in q
                            except Exception:
                                return "token=" in query

                        def _query_param(query: str, key: str) -> str | None:
                            if not isinstance(query, str) or not query or not key:
                                return None
                            try:
                                from urllib.parse import parse_qs

                                raw = query[1:] if query.startswith("?") else query
                                q = parse_qs(raw, keep_blank_values=True)
                                vals = q.get(key)
                                if isinstance(vals, list) and vals:
                                    v0 = str(vals[0]).strip()
                                    return v0 or None
                                if isinstance(vals, str):
                                    v0 = str(vals).strip()
                                    return v0 or None
                                return None
                            except Exception:
                                return None

                        def _mark_pending(key: str) -> None:
                            try:
                                st = pending_tunnel_meta.get(key)
                                now = time.monotonic()
                                if st is None:
                                    pending_tunnel_meta[key] = {"first_at": now, "last_at": now, "count": 1}
                                else:
                                    st["last_at"] = now
                                    st["count"] = int(st.get("count") or 0) + 1
                            except Exception:
                                pass

                        async def _maybe_force_close_no_upstream(key: str) -> None:
                            if _route_no_upstream_close_after_s <= 0:
                                return
                            try:
                                st = pending_tunnel_meta.get(key)
                                if not st:
                                    return
                                first_at = float(st.get("first_at") or 0.0)
                                if first_at <= 0:
                                    return
                                age = time.monotonic() - first_at
                                if age < _route_no_upstream_close_after_s:
                                    return
                                # Ask root to close this tunnel so it re-opens with an "open" handshake.
                                await _route_reply(key, {"t": "close", "err": "no_upstream"})
                                pending_tunnel_meta.pop(key, None)
                                if _route_trace:
                                    _route_log(
                                        f"[hub-route] forced close key={_key_tag(key)} age_s={age:.2f} reason=no_upstream"
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
                                last_recv_err = None
                                last_recv_err_ago_s = None
                                try:
                                    last_recv_err = getattr(tr, "_adaos_last_recv_error", None) if tr is not None else None
                                    last_recv_err_at = getattr(tr, "_adaos_last_recv_error_at", None) if tr is not None else None
                                    if isinstance(last_recv_err_at, (int, float)):
                                        last_recv_err_ago_s = round(time.monotonic() - float(last_recv_err_at), 3)
                                except Exception:
                                    last_recv_err = last_recv_err or None
                                    last_recv_err_ago_s = last_recv_err_ago_s or None

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
                                    f"last_recv_err={type(last_recv_err).__name__ if last_recv_err is not None else None} "
                                    f"last_recv_err_ago_s={last_recv_err_ago_s} "
                                    f"pending_data_size={pending_data_size} pings_outstanding={pings_outstanding} pongs_q={pongs_q}"
                                )
                            except Exception:
                                return ""

                        async def _route_reply(key: str, payload: dict[str, Any]) -> None:
                            reply_subject = f"route.to_browser.{key}"
                            reply_started = time.monotonic()
                            t0 = None
                            try:
                                t0 = (payload or {}).get("t")
                            except Exception:
                                t0 = None
                            if _route_http_trace and (t0 in ("http_resp", "close") or _route_frame_verbose):
                                try:
                                    _route_log(
                                        f"[hub-route] reply.start key={_key_tag(key)} subj={reply_subject} {_route_payload_summary(payload)}"
                                    )
                                except Exception:
                                    pass
                            try:
                                try:
                                    await asyncio.wait_for(
                                        nc.publish(
                                            reply_subject,
                                            _json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                        ),
                                        timeout=max(0.1, float(_route_send_timeout_s)),
                                    )
                                except asyncio.TimeoutError:
                                    raise RuntimeError("publish timeout")
                                if _route_http_trace and (t0 in ("http_resp", "close") or _route_frame_verbose):
                                    try:
                                        took_ms = (time.monotonic() - reply_started) * 1000.0
                                        _route_log(
                                            f"[hub-route] reply.published key={_key_tag(key)} subj={reply_subject} took_ms={took_ms:.1f} {_route_payload_summary(payload)}"
                                        )
                                    except Exception:
                                        pass
                                # Ensure the reply is actually flushed quickly; otherwise Root may time out
                                # waiting on `route.to_browser.<key>` (especially over websocket-proxied NATS).
                                try:
                                    t = (payload or {}).get("t")
                                    if _route_trace:
                                        try:
                                            if t in ("close", "http_resp") or (_route_frame_verbose and t in ("frame", "chunk")):
                                                status = (payload or {}).get("status")
                                                kind = (payload or {}).get("kind")
                                                size = None
                                                if t == "frame":
                                                    data = (payload or {}).get("data") or (payload or {}).get("data_b64")
                                                    try:
                                                        size = len(data) if data is not None else None
                                                    except Exception:
                                                        size = None
                                                if t == "chunk":
                                                    data = (payload or {}).get("data") or (payload or {}).get("data_b64")
                                                    try:
                                                        size = len(data) if data is not None else None
                                                    except Exception:
                                                        size = None
                                                _route_log(
                                                    f"[hub-route] tx t={t} key={_key_tag(key)} status={status} kind={kind} size={size}"
                                                )
                                        except Exception:
                                            pass
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
                                        elif _route_http_trace and t in ("http_resp", "close"):
                                            try:
                                                _route_log(
                                                    f"[hub-route] reply.flushed key={_key_tag(key)} subj={reply_subject} flush_ms={flush_took_s * 1000.0:.1f} {_route_payload_summary(payload)}"
                                                )
                                            except Exception:
                                                pass
                                except Exception:
                                    pass
                            except Exception as e:
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
                                if _route_http_trace:
                                    try:
                                        _route_log(
                                            f"[hub-route] reply.fail key={_key_tag(key)} subj={reply_subject} err={type(e).__name__}: {e} {_route_payload_summary(payload)} {_route_nc_diag()}"
                                        )
                                    except Exception:
                                        pass

                        def _hub_key_match(key: str) -> bool:
                            current_hub_id = hub_id
                            try:
                                cfg_now = load_config(ctx=self.ctx)
                                current_hub_id = str(getattr(cfg_now, "subnet_id", "") or current_hub_id)
                            except Exception:
                                current_hub_id = hub_id
                            try:
                                return isinstance(key, str) and bool(current_hub_id) and key.startswith(f"{current_hub_id}--")
                            except Exception:
                                return False

                        async def _tunnel_reader(key: str, ws) -> None:
                            try:
                                async for msg in ws:
                                    if _route_frame_verbose:
                                        try:
                                            if isinstance(msg, (bytes, bytearray)):
                                                _route_log(f"[hub-route] rx upstream frame key={_key_tag(key)} kind=bin size={len(msg)}")
                                            else:
                                                _route_log(
                                                    f"[hub-route] rx upstream frame key={_key_tag(key)} kind=text size={len(str(msg))}"
                                                )
                                        except Exception:
                                            pass
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
                            except Exception as e:
                                if _route_trace:
                                    try:
                                        _route_log(
                                            f"[hub-route] upstream reader error key={_key_tag(key)} err={type(e).__name__}: {e}"
                                        )
                                    except Exception:
                                        pass
                            finally:
                                if _route_trace:
                                    try:
                                        code = getattr(ws, "close_code", None)
                                        reason = getattr(ws, "close_reason", None)
                                        exc = None
                                        try:
                                            exf = getattr(ws, "exception", None)
                                            if callable(exf):
                                                exc = exf()
                                        except Exception:
                                            exc = None
                                        _route_log(
                                            f"[hub-route] upstream closed key={_key_tag(key)} code={code} reason={reason} exc={exc}"
                                        )
                                    except Exception:
                                        pass
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
                                    pending_tunnel_events.pop(key, None)
                                except Exception:
                                    pass
                                try:
                                    await ws.close()
                                except Exception:
                                    pass

                        def _queue_pending_tunnel_event(key: str, payload: dict[str, Any]) -> None:
                            try:
                                items = pending_tunnel_events.get(key)
                                if items is None:
                                    items = []
                                    pending_tunnel_events[key] = items
                                if len(items) >= MAX_PENDING_TUNNEL_EVENTS:
                                    items.pop(0)
                                items.append(dict(payload))
                            except Exception:
                                pass

                        async def _send_tunnel_event(key: str, ws, payload: dict[str, Any]) -> None:
                            kind = (payload or {}).get("t")
                            if kind == "frame":
                                frame_kind = (payload or {}).get("kind")
                                if frame_kind == "bin":
                                    b64 = (payload or {}).get("data_b64")
                                    if isinstance(b64, str) and b64:
                                        await asyncio.wait_for(
                                            ws.send(base64.b64decode(b64.encode("ascii"))),
                                            timeout=max(0.1, float(_route_upstream_ws_send_timeout_s)),
                                        )
                                else:
                                    txt = (payload or {}).get("data")
                                    if isinstance(txt, str):
                                        await asyncio.wait_for(
                                            ws.send(txt),
                                            timeout=max(0.1, float(_route_upstream_ws_send_timeout_s)),
                                        )
                                return

                            if kind != "chunk":
                                return

                            cid = (payload or {}).get("id")
                            idx = int((payload or {}).get("idx") or 0)
                            total = int((payload or {}).get("total") or 0)
                            frame_kind = "text" if (payload or {}).get("kind") == "text" else "bin"
                            if not isinstance(cid, str) or not cid or total <= 0 or idx < 0 or idx >= total:
                                return
                            st = pending_chunks.get(cid)
                            if not st:
                                st = {"key": key, "kind": frame_kind, "total": total, "parts": [None] * total}
                                pending_chunks[cid] = st
                            if st.get("key") != key or st.get("kind") != frame_kind or int(st.get("total") or 0) != total:
                                return
                            parts = st.get("parts")
                            if not isinstance(parts, list) or len(parts) != total:
                                st["parts"] = [None] * total
                                parts = st["parts"]
                            if frame_kind == "bin":
                                b64 = (payload or {}).get("data_b64")
                                if not isinstance(b64, str):
                                    return
                                parts[idx] = base64.b64decode(b64.encode("ascii"))
                            else:
                                txt = (payload or {}).get("data")
                                if not isinstance(txt, str):
                                    return
                                parts[idx] = txt
                            if any(p is None for p in parts):
                                return
                            pending_chunks.pop(cid, None)
                            if frame_kind == "bin":
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

                        async def _route_cb(msg) -> None:
                            key = ""
                            subject = ""
                            is_http_key = False
                            route_t = "?"
                            route_outcome = "start"
                            route_started = time.monotonic()
                            try:
                                subject = str(getattr(msg, "subject", "") or "")
                                parts = subject.split(".", 2)
                                # route.to_hub.<key>
                                if len(parts) < 3:
                                    route_outcome = "drop_bad_subject"
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
                                    route_outcome = "drop_key_mismatch"
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
                                    route_outcome = "drop_invalid_json"
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
                                    route_outcome = "drop_invalid_payload"
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
                                route_t = str(t or "?")
                                if not isinstance(t, str) or not t:
                                    route_outcome = "drop_missing_t"
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
                                if _route_http_trace and (is_http_key or t in ("open", "close")):
                                    try:
                                        _route_log(
                                            f"[hub-route] cb.start key={_key_tag(key)} subj={subject} bytes={len(raw)} {_route_payload_summary(data)}"
                                        )
                                    except Exception:
                                        pass
                                if _route_verbose or _route_trace:
                                    try:
                                        if t == "http":
                                            _m = str((data or {}).get("method") or "GET").upper()
                                            _p = str((data or {}).get("path") or "")
                                            if _p not in ("/api/node/status", "/api/ping", "/healthz"):
                                                _route_log(f"[hub-route] rx http key={_key_tag(key)} {_m} {_p}")
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
                                                _route_log(f"[hub-route] rx open key={_key_tag(key)} path={_p}")
                                        elif t == "close":
                                            _route_log(f"[hub-route] rx close key={_key_tag(key)}")
                                        else:
                                            # Frames are extremely noisy; enable explicitly when debugging.
                                            if t == "frame" and not _route_frame_verbose:
                                                pass
                                            else:
                                                _route_log(f"[hub-route] rx t={t} key={_key_tag(key)}")
                                    except Exception:
                                        pass

                                    if _route_trace:
                                        try:
                                            if t == "open":
                                                _p = str((data or {}).get("path") or "")
                                                _q = str((data or {}).get("query") or "")
                                                _dev = _query_param(_q, "dev")
                                                _wsq = _query_param(_q, "ws")
                                                _route_log(
                                                    f"[hub-route] open req key={_key_tag(key)} path={_p} query_len={len(_q)} token={_query_has_token(_q)} dev={_dev} ws={_wsq}"
                                                )
                                            elif t == "frame":
                                                _kind = (data or {}).get("kind")
                                                _size = None
                                                _body = (data or {}).get("data") or (data or {}).get("data_b64")
                                                try:
                                                    _size = len(_body) if _body is not None else None
                                                except Exception:
                                                    _size = None
                                                if _route_frame_verbose:
                                                    _route_log(
                                                        f"[hub-route] frame req key={_key_tag(key)} kind={_kind} size={_size}"
                                                    )
                                            elif t == "chunk":
                                                if _route_frame_verbose:
                                                    _route_log(
                                                        f"[hub-route] chunk req key={_key_tag(key)} idx={(data or {}).get('idx')} total={(data or {}).get('total')}"
                                                    )
                                            elif t == "close":
                                                _route_log(f"[hub-route] close req key={_key_tag(key)}")
                                        except Exception:
                                            pass

                                if t == "open":
                                    route_outcome = "open"
                                    # Open a local WS to the hub server and start pumping frames.
                                    if websockets_mod is None:
                                        route_outcome = "open_no_websockets"
                                        if _route_trace:
                                            _route_log(f"[hub-route] open upstream failed key={_key_tag(key)} err=websockets_unavailable")
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
                                    if _route_verbose or _route_trace:
                                        try:
                                            _route_log(f"[hub-route] open upstream url={url}")
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
                                        if _route_trace:
                                            _route_log(
                                                f"[hub-route] upstream.connect start key={_key_tag(key)} timeout_s={ws_connect_timeout_s}"
                                            )
                                        t0 = time.monotonic()
                                        ws = await asyncio.wait_for(
                                            websockets_mod.connect(url, max_size=None),
                                            timeout=ws_connect_timeout_s,
                                        )
                                        if _route_trace:
                                            took = time.monotonic() - t0
                                            proto = getattr(ws, "subprotocol", None) or getattr(ws, "protocol", None)
                                            remote = getattr(ws, "remote_address", None)
                                            _route_log(
                                                f"[hub-route] upstream.connect ok key={_key_tag(key)} took_s={took:.3f} proto={proto} remote={remote}"
                                            )
                                    except Exception as e:
                                        route_outcome = f"open_connect_fail:{type(e).__name__}"
                                        if _route_trace:
                                            _route_log(
                                                f"[hub-route] upstream.connect fail key={_key_tag(key)} err={type(e).__name__}: {e}"
                                            )
                                        await _route_reply(key, {"t": "close", "err": str(e)})
                                        return
                                    route_outcome = "open_connected"
                                    tunnels[key] = {"ws": ws, "url": url}
                                    pending_tunnel_meta.pop(key, None)
                                    tunnel_tasks[key] = asyncio.create_task(_tunnel_reader(key, ws), name=f"hub-route-{key}")
                                    pending = pending_tunnel_events.pop(key, None) or []
                                    for pending_payload in pending:
                                        try:
                                            await _send_tunnel_event(key, ws, pending_payload)
                                        except Exception as e:
                                            if _route_verbose or _route_trace:
                                                try:
                                                    _route_log(
                                                        f"[hub-route] flush pending failed key={_key_tag(key)}: {type(e).__name__}: {e}"
                                                    )
                                                except Exception:
                                                    pass
                                            break
                                    route_outcome = "open_ready"
                                    return

                                if t == "close":
                                    route_outcome = "close_local"
                                    rec = tunnels.pop(key, None)
                                    task = tunnel_tasks.pop(key, None)
                                    pending_tunnel_meta.pop(key, None)
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
                                    if _route_trace:
                                        _route_log(f"[hub-route] upstream close req key={_key_tag(key)}")
                                    return

                                if t == "frame":
                                    rec = tunnels.get(key)
                                    ws = rec.get("ws") if isinstance(rec, dict) else None
                                    if not ws:
                                        route_outcome = "frame_no_upstream"
                                        _queue_pending_tunnel_event(key, data)
                                        _mark_pending(key)
                                        await _maybe_force_close_no_upstream(key)
                                        if _route_trace:
                                            try:
                                                st = pending_tunnel_meta.get(key) or {}
                                                first_at = float(st.get("first_at") or 0.0)
                                                age_s = time.monotonic() - first_at if first_at > 0 else None
                                                count = st.get("count")
                                            except Exception:
                                                age_s = None
                                                count = None
                                            _route_log(
                                                f"[hub-route] queue frame key={_key_tag(key)} reason=no_upstream age_s={age_s} count={count}"
                                            )
                                        return
                                    try:
                                        await _send_tunnel_event(key, ws, data)
                                        route_outcome = "frame_sent"
                                    except Exception as e:
                                        route_outcome = f"frame_send_fail:{type(e).__name__}"
                                        if _route_verbose or _route_trace:
                                            try:
                                                _route_log(
                                                    f"[hub-route] ws.send(frame) failed key={_key_tag(key)}: {type(e).__name__}: {e}"
                                                )
                                            except Exception:
                                                pass
                                    return
                                
                                if t == "chunk":
                                    rec = tunnels.get(key)
                                    ws = rec.get("ws") if isinstance(rec, dict) else None
                                    if not ws:
                                        route_outcome = "chunk_no_upstream"
                                        _queue_pending_tunnel_event(key, data)
                                        _mark_pending(key)
                                        await _maybe_force_close_no_upstream(key)
                                        if _route_trace:
                                            try:
                                                st = pending_tunnel_meta.get(key) or {}
                                                first_at = float(st.get("first_at") or 0.0)
                                                age_s = time.monotonic() - first_at if first_at > 0 else None
                                                count = st.get("count")
                                            except Exception:
                                                age_s = None
                                                count = None
                                            _route_log(
                                                f"[hub-route] queue chunk key={_key_tag(key)} reason=no_upstream age_s={age_s} count={count}"
                                            )
                                        return
                                    try:
                                        await _send_tunnel_event(key, ws, data)
                                        route_outcome = "chunk_sent"
                                    except Exception as e:
                                        route_outcome = f"chunk_send_fail:{type(e).__name__}"
                                        if _route_verbose or _route_trace:
                                            try:
                                                _route_log(
                                                    f"[hub-route] ws.send(chunked) failed key={_key_tag(key)}: {type(e).__name__}: {e}"
                                                )
                                            except Exception:
                                                pass
                                    return

                                if t == "http":
                                    route_outcome = "http"
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
                                                route_outcome = "http_inline_probe_replied"
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
                                                if _route_diag:
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
                                            def _do_http_upstream() -> dict[str, Any]:
                                                sess = requests.Session()
                                                try:
                                                    try:
                                                        sess.trust_env = False
                                                    except Exception:
                                                        pass
                                                    last_exc: Exception | None = None
                                                    resp = None
                                                    for base in bases:
                                                        url_try = f"{base}{path}{search}"
                                                        try:
                                                            # Root times out fairly quickly while waiting for
                                                            # route.to_browser.* replies. Keep local proxy attempts
                                                            # short and, critically, run them off the event loop
                                                            # thread because the local hub HTTP server lives in this
                                                            # same process.
                                                            is_probe = path in ("/api/node/status", "/api/ping", "/healthz")
                                                            timeout = (0.5, 1.2) if is_probe else (1.5, 2.5)
                                                            resp = sess.request(method, url_try, data=body, headers=h2, timeout=timeout)
                                                            last_exc = None
                                                            break
                                                        except Exception as e:
                                                            last_exc = e
                                                            if _route_verbose:
                                                                try:
                                                                    print(
                                                                        f"[hub-route] http upstream failed url={url_try}: {type(e).__name__}: {e}"
                                                                    )
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
                                                finally:
                                                    try:
                                                        sess.close()
                                                    except Exception:
                                                        pass

                                            return _do_http_upstream()
                                        except Exception as e:
                                            return {"t": "http_resp", "status": 502, "headers": {}, "body_b64": "", "err": str(e)}

                                    resp = await asyncio.to_thread(_do_http)
                                    route_outcome = f"http_local_done:{resp.get('status')}"
                                    if _route_http_trace:
                                        try:
                                            _route_log(
                                                f"[hub-route] http.local.done key={_key_tag(key)} status={resp.get('status')} err={resp.get('err')} truncated={resp.get('truncated')}"
                                            )
                                        except Exception:
                                            pass
                                    try:
                                        await _route_reply(key, resp)
                                        route_outcome = f"http_replied:{resp.get('status')}"
                                    except Exception:
                                        pass
                                    return
                                # Unknown route message type: for HTTP keys, reply with an error so Root does not time out.
                                try:
                                    route_outcome = f"unsupported_t:{t}"
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
                                route_outcome = f"handler_failed:{type(e).__name__}"
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
                            finally:
                                if _route_http_trace and key:
                                    try:
                                        took_ms = (time.monotonic() - route_started) * 1000.0
                                        _route_log(
                                            f"[hub-route] cb.done key={_key_tag(key)} subj={subject} t={route_t} outcome={route_outcome} took_ms={took_ms:.1f}"
                                        )
                                    except Exception:
                                        pass
                                return

                        route_sub = await _sub("route.to_hub.*", cb=_route_cb)
                        if hub_nats_verbose or not hub_nats_quiet:
                            print("[hub-io] NATS subscribe route.to_hub.* (hub route proxy)")
                        try:
                            self._log.info("nats bridge subscribed subject=route.to_hub.*")
                        except Exception:
                            pass
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
                            try:
                                self._log.info("nats bridge subscribed subject=%s", alt)
                            except Exception:
                                pass
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
                                try:
                                    self._log.info("nats bridge subscribed subject=%s", alt_legacy)
                                except Exception:
                                    pass
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
                            try:
                                _write_nats_ws_diag_file(
                                    nc,
                                    server=nats_last_server,
                                    source="periodic",
                                )
                            except Exception:
                                pass
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
                                    if _tname == "_ping_interval_task" and bool(getattr(nc, "_adaos_ping_interval_task_disabled", False)):
                                        continue
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
                                        try:
                                            if _exc is None:
                                                tr = getattr(nc, "_transport", None)
                                                _le = getattr(tr, "_adaos_last_recv_error", None) if tr is not None else None
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
                                        try:
                                            self._log.warning(_msg)
                                        except Exception:
                                            pass
                                        _rl_log("nats.watchdog", _msg, every_s=1.0)
                                        try:
                                            _log_nats_ws_diag(
                                                nc,
                                                server=nats_last_server,
                                                rate_key="nats.ws_diag.watchdog",
                                                every_s=1.0,
                                                source="watchdog",
                                                task_name=_tname,
                                                err=_exc if isinstance(_exc, Exception) else None,
                                            )
                                        except Exception:
                                            pass
                                        if _exc is not None:
                                            raise RuntimeError(_msg) from _exc
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
                                try:
                                    self._log.warning(
                                        "nats bridge closed server=%s hub_id=%s details=%s",
                                        nats_last_server,
                                        hub_id,
                                        details,
                                    )
                                except Exception:
                                    pass
                                raise RuntimeError(f"nats connection closed{(': ' + details) if details else ''}")
                    finally:
                        try:
                            self._log.info("nats bridge finalizing hub_id=%s server=%s", hub_id, nats_last_server)
                        except Exception:
                            pass
                        def _keep_pending_task(task: asyncio.Task | None) -> None:
                            # asyncio keeps only weak refs to tasks; if we drop our references before a
                            # canceled task finishes, Python can emit "Task was destroyed but it is pending!".
                            try:
                                if not isinstance(task, asyncio.Task) or task.done():
                                    return
                            except Exception:
                                return
                            try:
                                alive = getattr(self, "_nats_pending_cleanup_tasks", None)
                                if alive is None:
                                    alive = set()
                                    setattr(self, "_nats_pending_cleanup_tasks", alive)
                                alive.add(task)

                                def _drop(done: asyncio.Task) -> None:
                                    try:
                                        alive.discard(done)
                                    except Exception:
                                        pass

                                task.add_done_callback(_drop)
                            except Exception:
                                pass
                        try:
                            if raw_keepalive_task is not None:
                                try:
                                    raw_keepalive_task.cancel()
                                except Exception:
                                    pass
                                try:
                                    _keep_pending_task(raw_keepalive_task)
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
                            # WebSocket transports can leave client resources unclosed
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
                            for task in list(sub_workers):
                                try:
                                    task.cancel()
                                except Exception:
                                    pass
                                try:
                                    _keep_pending_task(task if isinstance(task, asyncio.Task) else None)
                                except Exception:
                                    pass
                            if sub_workers:
                                try:
                                    await asyncio.wait_for(asyncio.gather(*sub_workers, return_exceptions=True), timeout=1.0)
                                except Exception:
                                    pass
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
                                    try:
                                        _keep_pending_task(t)
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

                async def _maybe_snapshot_root_logs(
                    *,
                    trace: bool,
                    force: bool = False,
                    tag_override: str | None = None,
                    server_override: str | None = None,
                ) -> None:
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

                            u = _urlparse(str(server_override or nats_last_server or ""))
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
                            snapshot_lines = int(os.getenv("HUB_ROOT_LOG_SNAPSHOT_LINES", "250") or "250")
                        except Exception:
                            snapshot_lines = 250
                        if snapshot_lines < 50:
                            snapshot_lines = 50

                        out_dir = Path(".adaos") / "root_log_snapshots"
                        out_dir.mkdir(parents=True, exist_ok=True)

                        def _fetch_one(fname: str) -> tuple[str, str]:
                            import urllib.parse as _up
                            import urllib.request as _ureq

                            qs = _up.urlencode({"file": fname, "lines": str(snapshot_lines)})
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
                                import re as _re

                                obj = _json.loads(body)
                                lines0 = obj.get("lines", [])
                                if not isinstance(lines0, list):
                                    return ""
                                tag_s = str(tag)
                                hub_prefix = tag_s.rsplit("-", 1)[0] if "-" in tag_s else tag_s
                                tag_hits = [str(s) for s in lines0 if isinstance(s, str) and tag_s in s]
                                conn_ids: set[str] = set()
                                for line0 in tag_hits:
                                    try:
                                        for m0 in _re.finditer(r'"conn":"([^"]+)"', line0):
                                            conn_ids.add(str(m0.group(1)))
                                    except Exception:
                                        continue
                                route_prefixes = (
                                    f"route.to_browser.{hub_prefix}--",
                                    f"route.to_hub.{hub_prefix}--",
                                )
                                include_extra = str(os.getenv("HUB_ROOT_LOG_SNAPSHOT_EXTRACT_EXTRA", "0") or "0").strip() == "1"
                                extra_keywords = (
                                    "http proxy failed",
                                    "ws tunnel:",
                                    "nats http route",
                                    "nats keepalive pong missing",
                                    "nats route chunk (client->proxy)",
                                    "nats route upstream write",
                                    "conn close",
                                    "upstream close",
                                    "upstream error",
                                    "ws close 1006 diag",
                                    "ws socket data after keepalive",
                                    "ws socket readable after keepalive",
                                    "ws socket pause",
                                    "ws socket resume",
                                    "ws socket end",
                                    "ws socket close",
                                    "ws socket error",
                                    "ws error",
                                    "ws upstream closed",
                                    "closing superseded hub ws-nats connection",
                                )
                                hits: list[str] = []
                                for item in lines0:
                                    if not isinstance(item, str):
                                        continue
                                    line = str(item)
                                    include = tag_s in line
                                    if not include and conn_ids:
                                        try:
                                            include = any(cid and cid in line for cid in conn_ids)
                                        except Exception:
                                            include = False
                                    if not include:
                                        try:
                                            include = any(pref in line for pref in route_prefixes)
                                        except Exception:
                                            include = False
                                    if include_extra and (not include):
                                        try:
                                            include = any(kw in line for kw in extra_keywords)
                                        except Exception:
                                            include = False
                                    if include:
                                        hits.append(line)
                                # Keep this file small and focused.
                                return "\n".join(hits[-1000:])
                            except Exception:
                                return ""

                        try:
                            if isinstance(tag_override, str) and tag_override.strip():
                                tag0 = tag_override.strip()
                            else:
                                tag0 = ws_connect_tag if isinstance(ws_connect_tag, str) else ""
                        except Exception:
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
                                        try:
                                            if os.getenv("HUB_ROOT_LOG_SNAPSHOT_EXTRACT_PRINT", "0") == "1":
                                                try:
                                                    tail_n = int(os.getenv("HUB_ROOT_LOG_SNAPSHOT_EXTRACT_TAIL", "40") or "40")
                                                except Exception:
                                                    tail_n = 40
                                                if tail_n < 1:
                                                    tail_n = 1
                                                tail_lines = ex.splitlines()
                                                tail = "\n".join(tail_lines[-tail_n:]) if tail_lines else ""
                                                if tail:
                                                    print(f"[hub-io] root log extract tail file={fn2} lines={len(tail_lines)}")
                                                    print(tail)
                                        except Exception:
                                            pass
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
                            try:
                                self._log.info("nats supervisor cancelled hub_id=%s server=%s", hub_id, nats_last_server)
                            except Exception:
                                pass
                            return
                        except Exception as e:
                            try:
                                self._log.warning(
                                    "nats supervisor error hub_id=%s server=%s type=%s err=%s",
                                    hub_id,
                                    nats_last_server,
                                    type(e).__name__,
                                    str(e),
                                )
                            except Exception:
                                pass
                            try:
                                print(f"[hub-io] nats: encountered error: {e}")
                            except Exception:
                                pass
                            try:
                                local_sidecar_url = realtime_sidecar_local_url()
                                using_sidecar = bool(
                                    isinstance(nats_last_server, str)
                                    and isinstance(local_sidecar_url, str)
                                    and str(nats_last_server).strip() == str(local_sidecar_url).strip()
                                )
                            except Exception:
                                using_sidecar = False
                            try:
                                if using_sidecar:
                                    async def _print_sidecar_tail() -> None:
                                        def _tail(path: Path, lines: int) -> tuple[Path, list[str]]:
                                            try:
                                                data = path.read_text(encoding="utf-8", errors="replace").splitlines()
                                            except Exception:
                                                data = []
                                            return path, data[-lines:]

                                        try:
                                            log_path, log_tail = await asyncio.to_thread(_tail, realtime_sidecar_log_path(), 40)
                                            if log_tail:
                                                print(f"[hub-io] adaos-realtime log tail file={log_path} lines={len(log_tail)}")
                                                print("\n".join(log_tail))
                                        except Exception:
                                            pass
                                        try:
                                            diag_path, diag_tail = await asyncio.to_thread(_tail, realtime_sidecar_diag_path(), 10)
                                            if diag_tail:
                                                print(f"[hub-io] adaos-realtime diag tail file={diag_path} lines={len(diag_tail)}")
                                                print("\n".join(diag_tail))
                                        except Exception:
                                            pass

                                    asyncio.create_task(_print_sidecar_tail(), name="adaos-realtime-log-tail")
                            except Exception:
                                pass
                            # Optional delayed snapshot: root-side logs (ECONNRESET/conn close) can be emitted
                            # slightly after the hub notices EOF. A second tail a few seconds later often captures it.
                            try:
                                # `HUB_ROOT_LOG_SNAPSHOT_AFTER_ERR_S` accepts a comma list of delays in seconds.
                                # Set it to empty to disable follow-up snapshots entirely.
                                after_env = os.getenv("HUB_ROOT_LOG_SNAPSHOT_AFTER_ERR_S")
                                if after_env is None:
                                    after_env = "0,3"
                            except Exception:
                                after_env = "0,3"
                            delays: list[float] = []
                            try:
                                if str(after_env or "").strip():
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
                                else:
                                    delays = []
                            except Exception:
                                delays = []
                            # Schedule snapshots in the background so reconnect is not delayed by HTTP tailing.
                            try:
                                if delays and os.getenv("HUB_ROOT_LOG_SNAPSHOT", "0") == "1":
                                    tag0 = ws_connect_tag if isinstance(ws_connect_tag, str) else None
                                    srv0 = nats_last_server if isinstance(nats_last_server, str) else None

                                    async def _snap_later(delay_s: float) -> None:
                                        try:
                                            if delay_s > 0:
                                                await asyncio.sleep(min(30.0, max(0.1, float(delay_s))))
                                        except Exception:
                                            pass
                                        try:
                                            await _maybe_snapshot_root_logs(
                                                trace=trace0,
                                                force=True,
                                                tag_override=tag0,
                                                server_override=srv0,
                                            )
                                        except Exception:
                                            pass

                                    for after_s in delays[:8]:
                                        try:
                                            asyncio.create_task(_snap_later(float(after_s)), name="adaos-root-log-snapshot")
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                            # No blocking snapshots here: supervisor keeps retrying promptly.
                            try:
                                delays = []
                            except Exception:
                                pass

                            ran_for_s = time.monotonic() - started_at
                            try:
                                low = str(e).lower()
                                is_transient = (
                                    type(e).__name__ in ("UnexpectedEOF", "ClientConnectionResetError", "ConnectionClosedError")
                                    or "unexpected eof" in low
                                    or "connection reset" in low
                                    or "clientconnectionreseterror" in low
                                    or "cannot write to closing transport" in low
                                    or "connectionclosed" in low
                                    or "no close frame received or sent" in low
                                    or "winerror 121" in low
                                )
                            except Exception:
                                is_transient = False

                            try:
                                auto_env = os.getenv("HUB_NATS_WS_AUTO_FALLBACK")
                                if auto_env is None:
                                    auto_fallback = False
                                else:
                                    auto_fallback = str(auto_env).strip().lower() not in ("0", "false", "off", "no")
                                if (
                                    auto_fallback
                                    and os.name == "nt"
                                    and (last_ws_transport or "").lower() == "websockets"
                                    and is_transient
                                ):
                                    if os.getenv("HUB_NATS_WS_IMPL", "").lower() != "aiohttp":
                                        os.environ["HUB_NATS_WS_IMPL"] = "aiohttp"
                                        try:
                                            self._log.warning(
                                                "nats ws auto-fallback: switching to aiohttp transport after %s",
                                                type(e).__name__,
                                            )
                                        except Exception:
                                            pass
                                        try:
                                            print("[hub-io] nats ws auto-fallback -> aiohttp (HUB_NATS_WS_AUTO_FALLBACK=1)")
                                        except Exception:
                                            pass
                            except Exception:
                                pass

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
