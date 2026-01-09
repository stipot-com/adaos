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
from adaos.services.interpreter import registry as _interpreter_registry  # ensure interpreter NLU subscriptions
from adaos.services.interpreter import router_runtime as _interpreter_router  # ensure interpreter router subscriptions
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
        # Attach chat IO -> NLU bridge (e.g. Telegram text -> nlp.intent.detect)
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
        await register_subscriptions()
        await bus.emit("sys.bus.ready", {}, source="lifecycle", actor="system")
        # Start in-process scheduler after the bus is ready.
        try:
            await start_scheduler()
        except Exception:
            self._log.warning("failed to start scheduler", exc_info=True)
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

                bot_id = "main-bot"  # one-bot assumption for MVP
                sender = TelegramSender(bot_id)

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

                await self._io_bus.subscribe_output(bot_id, _handler)
        except Exception:
            pass

        # Inbound bridge from root NATS -> local event bus (tg.input.<hub_id>)
        try:
            # Hot-reload friendly: read NATS config from node.yaml on every connect attempt.
            hub_id = (getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)).subnet_id
            if hub_id:

                # Track connectivity state to log/emit only on transitions
                reported_down = False

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

                async def _nats_bridge() -> None:
                    nonlocal reported_down
                    backoff = 1.0

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
                                        if not pr0.path or pr0.path == "/":
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
                                    # Prefer WS endpoints only. Always include provided base (even api.inimatic.com)
                                    if base:
                                        _dedup_push(base)
                                        # Avoid generating trailing slash variants which may 400
                                        # Also try common WS mounts. Different deployments may expose the WS proxy
                                        # either on "/" (default) or on "/nats" (legacy).
                                        try:
                                            pr2 = urlparse(base)
                                            if (pr2.scheme or "").startswith("ws"):
                                                # hosts to try: configured host + known public aliases
                                                host_candidates: List[str] = []
                                                try:
                                                    if pr2.hostname:
                                                        host_candidates.append(pr2.hostname)
                                                except Exception:
                                                    pass
                                                for h in ("nats.inimatic.com", "api.inimatic.com"):
                                                    if h not in host_candidates:
                                                        host_candidates.append(h)

                                                # paths to try: keep configured path plus common ones
                                                path_candidates: List[str] = []
                                                pth = pr2.path or ""
                                                if pth and pth != "/":
                                                    path_candidates.append(pth)
                                                for p in ("/", "/nats"):
                                                    if p not in path_candidates:
                                                        path_candidates.append(p)

                                                for h in host_candidates:
                                                    for p in path_candidates:
                                                        prx = pr2._replace(netloc=h, path=p, params="", query="", fragment="")
                                                        _dedup_push(urlunparse(prx))
                                        except Exception:
                                            pass
                                    # Known public endpoint as a fallback
                                    _dedup_push("wss://nats.inimatic.com")
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

                            async def _on_error_cb(e: Exception) -> None:
                                # Best-effort; keep quiet unless explicitly verbose or useful
                                if os.getenv("SILENCE_NATS_EOF", "0") == "1" and (type(e).__name__ == "UnexpectedEOF" or "unexpected eof" in str(e).lower()):
                                    return
                                try:
                                    verbose = os.getenv("HUB_NATS_VERBOSE", "0") == "1"
                                    quiet = os.getenv("HUB_NATS_QUIET", "1") == "1"
                                    if quiet and not verbose:
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

                            nc = await _nats.connect(
                                servers=[str(s) for s in candidates],
                                user=user_str,
                                password=pw_str,
                                name=f"hub-{hub_id_str}",
                                error_cb=_on_error_cb,
                                disconnected_cb=_on_disconnected,
                                reconnected_cb=_on_reconnected,
                            )
                            subj = f"tg.input.{hub_id}"
                            subj_legacy = f"io.tg.in.{hub_id}.text"
                            print(f"[hub-io] NATS subscribe {subj} and legacy {subj_legacy}")
                            # First successful connect after failures
                            _emit_up()

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

                                await nc.subscribe(ctl_alias, cb=_ctl_alias_cb)
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
                                media_path = str(dest)
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

                    await nc.subscribe(subj, cb=cb)

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

                        async def _route_reply(key: str, payload: dict[str, Any]) -> None:
                            try:
                                await nc.publish(
                                    f"route.to_browser.{key}",
                                    _json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                )
                                # Ensure the reply is actually flushed quickly; otherwise Root may time out
                                # waiting on `route.to_browser.<key>` (especially over websocket-proxied NATS).
                                try:
                                    t = (payload or {}).get("t")
                                    if t in ("http_resp", "close"):
                                        await nc.flush(timeout=0.8)
                                        if _route_verbose:
                                            try:
                                                print(f"[hub-route] tx {t} key={key}")
                                            except Exception:
                                                pass
                                except Exception:
                                    pass
                            except Exception as e:
                                if _route_verbose:
                                    try:
                                        print(f"[hub-route] publish to_browser failed key={key}: {type(e).__name__}: {e}")
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
                            try:
                                subject = str(getattr(msg, "subject", "") or "")
                                parts = subject.split(".", 2)
                                # route.to_hub.<key>
                                if len(parts) < 3:
                                    return
                                key = parts[2]
                                if not _hub_key_match(key):
                                    return

                                try:
                                    data = _json.loads(msg.data.decode("utf-8"))
                                except Exception:
                                    data = {}
                                t = (data or {}).get("t")
                                if _route_verbose:
                                    try:
                                        if t == "http":
                                            _m = str((data or {}).get("method") or "GET").upper()
                                            _p = str((data or {}).get("path") or "")
                                            if _p not in ("/api/node/status", "/api/ping"):
                                                print(f"[hub-route] rx http key={key} {_m} {_p}")
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
                                        ws = await websockets_mod.connect(url, max_size=None)
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
                                                await ws.send(base64.b64decode(b64.encode("ascii")))
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
                                                await ws.send(txt)
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
                                            await ws.send(blob)
                                        else:
                                            await ws.send("".join([p for p in parts if isinstance(p, str)]))
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
                                    search = str((data or {}).get("search") or "")
                                    headers = (data or {}).get("headers") or {}
                                    body_b64 = (data or {}).get("body_b64")

                                    def _do_http() -> dict[str, Any]:
                                        try:
                                            import requests  # type: ignore

                                            try:
                                                from adaos.services.node_config import load_config

                                                cfg = getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)
                                                base_http = (
                                                    os.getenv("ADAOS_SELF_BASE_URL")
                                                    or str(getattr(cfg, "hub_url", None) or "")
                                                    or "http://127.0.0.1:8777"
                                                ).rstrip("/")
                                                base_http = base_http.replace("://0.0.0.0:", "://127.0.0.1:").replace("://[::]:", "://127.0.0.1:")
                                                token_local = getattr(cfg, "token", None) or os.getenv("ADAOS_TOKEN", "") or None
                                            except Exception:
                                                base_http = "http://127.0.0.1:8777"
                                                token_local = os.getenv("ADAOS_TOKEN", "") or None

                                            # Build candidate bases. Some setups expose a gateway on 8777 (sentinel)
                                            # while the actual core runs on another port (often 8788). If the default
                                            # base isn't reachable, retry with the target port.
                                            bases = [base_http]
                                            try:
                                                from urllib.parse import urlparse

                                                u0 = urlparse(base_http)
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
                                                    resp = sess.request(method, url_try, data=body, headers=h2, timeout=12)
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
                                    await _route_reply(key, resp)
                                    return
                            except Exception as e:
                                if _route_verbose:
                                    try:
                                        print(f"[hub-route] handler failed key={key}: {type(e).__name__}: {e}")
                                    except Exception:
                                        pass
                                return

                        route_sub = await nc.subscribe("route.to_hub.*", cb=_route_cb)
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
                            print(f"[hub-io] NATS subscribe (alias) {alt}")
                            await nc.subscribe(alt, cb=cb)
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
                            await nc.subscribe(subj_legacy, cb=cb_legacy)
                            aliases_env = os.getenv("HUB_INPUT_ALIASES", "")
                            aliases: List[str] = [a.strip() for a in aliases_env.split(",") if a.strip()]
                            seen = set([hub_id])
                            for aid in aliases:
                                if aid in seen:
                                    continue
                                seen.add(aid)
                                alt_legacy = f"io.tg.in.{aid}.text"
                                print(f"[hub-io] NATS subscribe (alias legacy) {alt_legacy}")
                                await nc.subscribe(alt_legacy, cb=cb_legacy)
                    except Exception:
                        pass
                    # keep task alive
                    try:
                        while True:
                            await asyncio.sleep(3600)
                    finally:
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
                            unsub = route_sub.unsubscribe()
                            if asyncio.iscoroutine(unsub):
                                await unsub
                        except Exception:
                            pass
                        try:
                            await nc.drain()
                        except Exception:
                            try:
                                await nc.close()
                            except Exception:
                                pass

                # Supervisor wrapper: never crash on unhandled errors; restart with backoff
                async def _nats_bridge_supervisor() -> None:
                    delay = 1.0
                    while True:
                        try:
                            await _nats_bridge()
                            # If bridge returns cleanly, keep it alive
                            await asyncio.sleep(3600)
                        except Exception as e:
                            try:
                                print(f"[hub-io] nats: encountered error: {e}")
                            except Exception:
                                pass
                            await asyncio.sleep(delay)
                            delay = min(delay * 2.0, 30.0)

                # TODO restore nats WS subscription
                self._boot_tasks.append(asyncio.create_task(_nats_bridge_supervisor(), name="adaos-nats-io-bridge"))
        except Exception:
            pass

    async def shutdown(self) -> None:
        await bus.emit("sys.stopping", {}, source="lifecycle", actor="system")
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
