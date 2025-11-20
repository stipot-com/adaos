# src\adaos\services\bootstrap.py
from __future__ import annotations
import asyncio, socket, time, uuid, os, logging, traceback
from typing import Any, List, Optional, Sequence
from pathlib import Path
import json as _json

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.sdk.data import bus
from adaos.services.node_config import load_config, set_role as cfg_set_role, NodeConfig
from adaos.services.eventbus import LocalEventBus
from adaos.services.io_bus.local_bus import LocalIoBus
from adaos.services.io_bus.http_fallback import HttpFallbackBus
from adaos.ports.heartbeat import HeartbeatPort
from adaos.ports.skills_loader import SkillsLoaderPort
from adaos.ports.subnet_registry import SubnetRegistryPort
from adaos.adapters.db.sqlite_schema import ensure_schema
from adaos.adapters.skills.git_repo import GitSkillRepository
from adaos.adapters.scenarios.git_repo import GitScenarioRepository
from adaos.sdk.core.decorators import register_subscriptions
from adaos.services.scheduler import start_scheduler
from adaos.apps.yjs import y_store as _y_store  # ensure YStore subscriptions are registered
from adaos.integrations.telegram.sender import TelegramSender
from adaos.services.chat_io.interfaces import ChatOutputEvent, ChatOutputMessage
from adaos.services.chat_io import telemetry as tm
import nats as _nats
from adaos.domain import Event


class BootstrapService:
    def __init__(self, ctx: AgentContext, *, heartbeat: HeartbeatPort, skills_loader: SkillsLoaderPort, subnet_registry: SubnetRegistryPort) -> None:
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
            # Guard: only enable NATS bridge if node.yaml contains an explicit 'nats' section on hub
            enable_nats = False
            node_nats: dict | None = None
            try:
                from adaos.services.capacity import _load_node_yaml as _load_node
                nd = _load_node()
                node_nats = (nd or {}).get("nats") if isinstance(nd, dict) else None
                if isinstance(node_nats, dict) and node_nats:
                    enable_nats = True
            except Exception:
                enable_nats = False

            if not enable_nats:
                # Do not attempt to connect using env defaults when node.yaml lacks 'nats'
                print("[hub-io] NATS disabled: no 'nats' config in node.yaml")
                return

            # prefer env, fallback to node.yaml 'nats.ws_url' (but only when 'nats' exists)
            nurl = os.getenv("NATS_WS_URL") or getattr(self.ctx.settings, "nats_url", None)
            nuser = os.getenv("NATS_USER") or None
            npass = os.getenv("NATS_PASS") or None
            # Prefer node.yaml values over env to avoid mixing
            try:
                nc = node_nats or {}
                node_ws = nc.get("ws_url")
                if node_ws:
                    nurl = node_ws
                nuser = nc.get("user") or nuser
                npass = nc.get("pass") or npass
            except Exception:
                pass
            hub_id = (getattr(self.ctx, "config", None) or load_config(ctx=self.ctx)).subnet_id
            if nurl and hub_id:

                # Track connectivity state to log/emit only on transitions
                reported_down = False

                async def _nats_bridge() -> None:
                    nonlocal reported_down
                    backoff = 1.0

                    def _explain_connect_error(err: Exception) -> str:
                        try:
                            msg = str(err) or ""
                            low = msg.lower()
                            if isinstance(err, TypeError) and "argument of type 'int' is not iterable" in low:
                                return (
                                    "root nats authentication error: WS closed after CONNECT; "
                                    "verify node.yaml nats.user=hub_<subnet_id> and nats.pass=<hub_nats_token>"
                                )
                        except Exception:
                            pass
                        # fallback – include class and message
                        try:
                            return f"{type(err).__name__}: {str(err)}"
                        except Exception:
                            return type(err).__name__
                    while True:
                        try:
                            user = nuser or os.getenv("NATS_USER") or None
                            pw = npass or os.getenv("NATS_PASS") or None
                            pw_mask = (pw[:3] + "***" + pw[-2:]) if pw and len(pw) > 6 else ("***" if pw else None)
                            # Build candidates without mixing WS and TCP schemes to avoid client errors.
                            candidates: List[str] = []

                            def _dedup_push(url: str) -> None:
                                if url and url not in candidates:
                                    candidates.append(url)

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

                            print(f"[hub-io] Connecting NATS candidates={candidates} user={user} pass={pw_mask}")

                            def _emit_down(kind: str, err: Exception | None) -> None:
                                nonlocal reported_down
                                if not reported_down:
                                    et = type(err).__name__ if err else kind
                                    # Produce a richer one-time diagnostics line to aid debugging WS/TLS/DNS issues
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
                                    try:
                                        print("[hub-io] nats connection restored")
                                    except Exception:
                                        pass
                                    try:
                                        self.ctx.bus.publish(
                                            Event(type="subnet.nats.up", payload={"ts": time.time()}, source="io.nats")
                                        )
                                    except Exception:
                                        pass
                                    reported_down = False

                            async def _on_error_cb(e: Exception) -> None:
                                # Best-effort; keep quiet unless explicitly verbose or useful
                                if os.getenv("SILENCE_NATS_EOF", "0") == "1" and (
                                    type(e).__name__ == "UnexpectedEOF" or "unexpected eof" in str(e).lower()
                                ):
                                    return
                                try:
                                    verbose = os.getenv("HUB_NATS_VERBOSE", "0") == "1"
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
                                        self.ctx.bus.publish(
                                            Event(type="subnet.nats.up", payload={"ts": time.time()}, source="io.nats")
                                        )
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
                                    if not (os.getenv("SILENCE_NATS_EOF", "0") == "1" and (
                                        type(e).__name__ == "UnexpectedEOF" or "unexpected eof" in str(e).lower()
                                    )):
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
                    while True:
                        await asyncio.sleep(3600)

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
