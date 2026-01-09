from __future__ import annotations

from typing import Any, Callable
from pathlib import Path
import asyncio
import json
import time
import requests
import os
import shutil
import subprocess
import sys
import y_py as Y

from adaos.services.eventbus import LocalEventBus
import logging
from adaos.domain import Event
from adaos.services.agent_context import get_ctx
from adaos.services.node_config import load_config
from .rules_loader import load_rules, watch_rules
from adaos.services.registry.subnet_directory import get_directory
from adaos.services.io_console import print_text
from adaos.sdk.data.env import get_tts_backend
from adaos.adapters.audio.tts.native_tts import NativeTTS
from adaos.integrations.rhasspy.tts import RhasspyTTSAdapter
from adaos.services.yjs.doc import async_get_ydoc
from adaos.skills.runtime_runner import execute_tool


class RouterService:
    def __init__(self, eventbus: LocalEventBus, base_dir: Path) -> None:
        self.bus = eventbus
        self.base_dir = base_dir
        self._started = False
        self._stop_watch: Callable[[], None] | None = None
        self._rules: list[dict[str, Any]] = []
        self._subscribed = False
        self._vlog = logging.getLogger("adaos.router.voice_chat")

    def _pick_target_node(self, desired_io: str, this_node: str) -> str:
        node = this_node
        for r in self._rules:
            try:
                target = r.get("target") or {}
                if str(target.get("io_type") or "stdout").lower() == desired_io.lower():
                    nid = target.get("node_id")
                    if nid == "this" or not nid:
                        node = this_node
                    else:
                        node = str(nid)
                    break
            except Exception:
                continue
        return node

    def _has_rule_for(self, desired_io: str) -> bool:
        for r in self._rules:
            try:
                target = r.get("target") or {}
                if str(target.get("io_type") or "").lower() == desired_io.lower():
                    return True
            except Exception:
                continue
        return False

    def _on_event(self, ev: Event) -> None:
        payload = ev.payload or {}
        text = (payload or {}).get("text")
        if not isinstance(text, str) or not text:
            return

        conf = get_ctx().config
        this_node = conf.node_id
        if not self._rules:
            try:
                self._rules = load_rules(self.base_dir, this_node)
            except Exception:
                pass
        # Multi-target routing: attempt telegram and stdout independently if rules exist
        did_any = False

        # Telegram route (if configured in rules)
        if self._has_rule_for("telegram"):
            target_node_tg = self._pick_target_node("telegram", this_node)
            try:
                # Resolve hub_id for target node
                if target_node_tg == this_node:
                    hub_id = conf.subnet_id
                else:
                    directory = get_directory()
                    node = directory.get_node(target_node_tg)
                    hub_id = (node or {}).get("subnet_id")
                if not hub_id:
                    raise RuntimeError("hub_id unresolved for telegram routing")
                # Root API base
                from adaos.services.agent_context import get_ctx as _get_ctx

                api_base = getattr(_get_ctx().settings, "api_base", "https://api.inimatic.com")
                url = f"{api_base.rstrip('/')}/io/tg/send"
                # Prefix message with subnet alias (or id) for clarity
                try:
                    from adaos.services.capacity import _load_node_yaml as _load_node

                    node_yaml = _load_node()
                except Exception:
                    node_yaml = {}
                try:
                    alias = ((node_yaml.get("nats") or {}).get("alias")) or os.getenv("DEFAULT_HUB") or conf.subnet_id
                except Exception:
                    alias = conf.subnet_id
                prefixed_text = f"[{alias}]: {text}" if alias else text
                body = {"hub_id": hub_id, "text": prefixed_text}
                try:
                    r = requests.post(url, json=body, headers={"Content-Type": "application/json"}, timeout=3.0)
                    logging.getLogger("adaos.router").info("router: telegram sent", extra={"hub_id": hub_id, "status": r.status_code})
                except Exception as pe:
                    logging.getLogger("adaos.router").warning("router: telegram request failed", extra={"hub_id": hub_id, "error": str(pe)})
                    raise
                did_any = True
            except Exception:
                # swallow to allow stdout route below
                try:
                    logging.getLogger("adaos.router").warning("router: telegram route failed; will continue with other routes")
                except Exception:
                    pass

        # Stdout route (if configured in rules)
        if self._has_rule_for("stdout"):
            target_node_out = self._pick_target_node("stdout", this_node)
            if target_node_out == this_node:
                print_text(text, node_id=this_node, origin={"source": ev.source})
                did_any = True
            else:
                # Cross-node delivery: resolve base_url and POST
                base_url = self._resolve_node_base_url(target_node_out, conf.role, conf.hub_url)
                if not base_url and conf.role == "hub":
                    try:
                        directory = get_directory()
                        candidates = []
                        for n in directory.list_known_nodes():
                            if not n.get("online"):
                                continue
                            for io in (n.get("capacity") or {}).get("io", []):
                                if io.get("io_type") == "stdout":
                                    candidates.append((int(io.get("priority") or 50), n))
                                    break
                        candidates.sort(key=lambda x: x[0], reverse=True)
                        for _, cand in candidates:
                            nid = cand.get("node_id")
                            if not nid:
                                continue
                            base_url = self._resolve_node_base_url(str(nid), conf.role, conf.hub_url)
                            if base_url:
                                break
                    except Exception:
                        base_url = None

                if base_url:
                    url = f"{base_url.rstrip('/')}/api/io/console/print"
                    headers = {"X-AdaOS-Token": conf.token or "dev-local-token", "Content-Type": "application/json"}
                    body = {"text": text, "origin": {"source": ev.source, "from": this_node}}
                    try:
                        requests.post(url, json=body, headers=headers, timeout=2.5)
                        did_any = True
                    except Exception:
                        pass
                else:
                    try:
                        logging.getLogger("adaos.router").warning(f"router: stdout target {target_node_out} offline/unresolved; fallback to local print")
                    except Exception:
                        pass
                    print_text(text, node_id=this_node, origin={"source": ev.source})
                    did_any = True

        # If no route matched or everything failed, fallback to local stdout
        if not did_any:
            print_text(text, node_id=this_node, origin={"source": ev.source})

    def _resolve_node_base_url(self, node_id: str, role: str, hub_url: str | None) -> str | None:
        try:
            if role == "hub":
                directory = get_directory()
                if not directory.is_online(node_id):
                    return None
                return directory.get_node_base_url(node_id)
            # member: ask hub
            if not hub_url:
                return None
            url = f"{hub_url.rstrip('/')}/api/subnet/nodes/{node_id}"
            token = load_config().token or "dev-local-token"
            r = requests.get(url, headers={"X-AdaOS-Token": token}, timeout=2.5)
            if r.status_code != 200:
                return None
            data = r.json() or {}
            node = data.get("node") or {}
            return node.get("base_url")
        except Exception:
            return None

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        # Subscribe to ui.notify on local event bus
        if not self._subscribed:
            self.bus.subscribe("ui.notify", self._on_event)

            # ui.say routing (TTS)
            def _say_via_system(text: str) -> bool:
                try:
                    if sys.platform.startswith("win"):
                        safe = text.replace("'", "''")
                        cmd = [
                            "powershell",
                            "-NoProfile",
                            "-Command",
                            "Add-Type -AssemblyName System.Speech; "
                            "$speak = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                            f"$speak.Speak('{safe}');",
                        ]
                        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return True
                    if sys.platform == "darwin" and shutil.which("say"):
                        subprocess.run(["say", text], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return True
                    if shutil.which("espeak"):
                        subprocess.run(["espeak", text], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return True
                except Exception:
                    return False
                return False

            def _on_say(ev: Event) -> None:
                text = ""
                try:
                    payload = ev.payload or {}
                    text = (payload or {}).get("text")
                    if not isinstance(text, str) or not text:
                        return
                    voice = (payload or {}).get("voice")
                    conf = load_config()
                    this_node = conf.node_id
                    target_node = self._pick_target_node("say", this_node)
                    base_url = self._resolve_node_base_url(target_node, conf.role, conf.hub_url)
                    token = conf.token or "dev-local-token"
                    if base_url and target_node != this_node:
                        try:
                            requests.post(
                                f"{base_url.rstrip('/')}/api/say",
                                json={"text": text, "voice": voice},
                                headers={"X-AdaOS-Token": token, "Content-Type": "application/json"},
                                timeout=3.0,
                            )
                            return
                        except Exception:
                            pass
                    # local fallback via API if self base_url known, else direct adapter
                    self_url = os.environ.get("ADAOS_SELF_BASE_URL")
                    if self_url:
                        try:
                            requests.post(
                                f"{self_url.rstrip('/')}/api/say",
                                json={"text": text, "voice": voice},
                                headers={"X-AdaOS-Token": token, "Content-Type": "application/json"},
                                timeout=3.0,
                            )
                            return
                        except Exception:
                            pass
                    try:
                        mode = get_tts_backend()
                        adapter = NativeTTS() if mode == "native" else RhasspyTTSAdapter()
                        adapter.say(text)
                    except Exception:
                        if not _say_via_system(text):
                            print_text(text, node_id=this_node, origin={"source": ev.source})
                except Exception:
                    try:
                        conf = get_ctx().config
                        print_text(text if isinstance(text, str) else "", node_id=conf.node_id, origin={"source": ev.source})
                    except Exception:
                        pass

            self.bus.subscribe("ui.say", _on_say)
            self._subscribed = True

        # ------------------------------------------------------------
        # Web voice chat routing (per-webspace)
        # ------------------------------------------------------------

        def _coerce_y(node: Any) -> Any:
            if isinstance(node, dict):
                return {str(k): _coerce_y(v) for k, v in node.items()}
            if isinstance(node, Y.YMap):
                return {str(k): _coerce_y(node.get(k)) for k in list(node.keys())}
            if isinstance(node, Y.YArray):
                return [_coerce_y(it) for it in node]
            return node

        def _resolve_webspace_ids_basic(payload: dict | None) -> list[str]:
            if not isinstance(payload, dict):
                return ["default"]

            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            raw_ids = (meta or {}).get("webspace_ids")
            if isinstance(raw_ids, list):
                out: list[str] = []
                for v in raw_ids:
                    s = str(v or "").strip()
                    if not s:
                        continue
                    if s not in out:
                        out.append(s)
                if out:
                    return out

            raw = (
                (meta or {}).get("webspace_id")
                or (meta or {}).get("workspace_id")
                or payload.get("webspace_id")
                or payload.get("workspace_id")
                or "default"
            )
            ws = str(raw or "").strip()
            return [ws or "default"]

        _route_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}

        async def _resolve_webspace_ids(payload: dict | None) -> list[str]:
            base_ids = _resolve_webspace_ids_basic(payload)
            if not isinstance(payload, dict):
                return base_ids

            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            # If explicit targets are provided, keep them authoritative.
            raw_ids = (meta or {}).get("webspace_ids")
            if isinstance(raw_ids, list) and raw_ids:
                return base_ids

            route_id = (meta or {}).get("route_id") or (meta or {}).get("route")
            if not isinstance(route_id, str) or not route_id.strip():
                return base_ids
            route_id = route_id.strip()
            src_ws = base_ids[0] if base_ids else "default"

            cached = _route_cache.get((src_ws, route_id))
            now = time.time()
            if cached and (now - cached[0]) < 1.0:
                return cached[1]

            try:
                async with async_get_ydoc(src_ws) as ydoc:
                    data = ydoc.get_map("data")
                    routing = _coerce_y(data.get("routing")) or {}
                    routes = routing.get("routes") if isinstance(routing, dict) else {}
                    if not isinstance(routes, dict):
                        routes = {}
                    entry = routes.get(route_id)
                    targets: list[str] = []
                    if isinstance(entry, list):
                        targets = [str(x).strip() for x in entry if str(x).strip()]
                    elif isinstance(entry, dict):
                        raw = entry.get("webspace_ids") or entry.get("targets")
                        if isinstance(raw, list):
                            targets = [str(x).strip() for x in raw if str(x).strip()]
                    if targets:
                        # De-dup while preserving order.
                        dedup: list[str] = []
                        for t in targets:
                            if t not in dedup:
                                dedup.append(t)
                        _route_cache[(src_ws, route_id)] = (now, dedup)
                        return dedup
            except Exception:
                pass

            _route_cache[(src_ws, route_id)] = (now, base_ids)
            return base_ids

        async def _ensure_voice_chat_state(webspace_id: str) -> None:
            async with async_get_ydoc(webspace_id) as ydoc:
                data_map = ydoc.get_map("data")
                current = data_map.get("voice_chat")
                if isinstance(current, dict) and isinstance(current.get("messages"), list):
                    return
                with ydoc.begin_transaction() as txn:
                    data_map.set(txn, "voice_chat", {"messages": []})

        async def _append_voice_chat_message(webspace_id: str, msg: dict) -> None:
            async with async_get_ydoc(webspace_id) as ydoc:
                data_map = ydoc.get_map("data")
                current = data_map.get("voice_chat")
                messages = []
                if isinstance(current, dict) and isinstance(current.get("messages"), list):
                    messages = list(current.get("messages") or [])
                messages.append(msg)
                # keep last N messages only (MVP)
                if len(messages) > 60:
                    messages = messages[-60:]
                with ydoc.begin_transaction() as txn:
                    data_map.set(txn, "voice_chat", {"messages": messages})
                try:
                    self._vlog.debug(
                        "voice_chat.append webspace=%s count=%d last_from=%s last_text=%r",
                        webspace_id,
                        len(messages),
                        msg.get("from"),
                        msg.get("text"),
                    )
                except Exception:
                    pass

        async def _ensure_tts_state(webspace_id: str) -> None:
            async with async_get_ydoc(webspace_id) as ydoc:
                data_map = ydoc.get_map("data")
                current = data_map.get("tts")
                if isinstance(current, dict) and isinstance(current.get("queue"), list):
                    return
                with ydoc.begin_transaction() as txn:
                    data_map.set(txn, "tts", {"queue": []})

        async def _append_tts_queue_item(webspace_id: str, item: dict) -> None:
            async with async_get_ydoc(webspace_id) as ydoc:
                data_map = ydoc.get_map("data")
                current = data_map.get("tts")
                queue = []
                if isinstance(current, dict) and isinstance(current.get("queue"), list):
                    queue = list(current.get("queue") or [])
                queue.append(item)
                if len(queue) > 50:
                    queue = queue[-50:]
                with ydoc.begin_transaction() as txn:
                    data_map.set(txn, "tts", {"queue": queue})

        def _now_ms() -> int:
            return int(time.time() * 1000)

        def _make_id(prefix: str) -> str:
            return f"{prefix}.{_now_ms()}"

        async def _on_voice_open(ev: Event) -> None:
            payload = ev.payload or {}
            for ws in await _resolve_webspace_ids(payload):
                await _ensure_voice_chat_state(ws)
                await _ensure_tts_state(ws)

        async def _on_io_out_chat_append(ev: Event) -> None:
            payload = ev.payload or {}
            if not isinstance(payload, dict):
                return
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                return
            msg = {
                "id": str(payload.get("id") or _make_id("m")),
                "from": str(payload.get("from") or "hub"),
                "text": text.strip(),
                "ts": float(payload.get("ts") or time.time()),
            }
            targets = await _resolve_webspace_ids(payload)
            try:
                self._vlog.debug(
                    "io.out.chat.append received text=%r from=%s targets=%s",
                    msg["text"],
                    msg["from"],
                    targets,
                )
            except Exception:
                pass
            for ws in targets:
                await _ensure_voice_chat_state(ws)
                await _append_voice_chat_message(ws, msg)

        async def _on_io_out_say(ev: Event) -> None:
            payload = ev.payload or {}
            if not isinstance(payload, dict):
                return
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                return
            item = {
                "id": str(payload.get("id") or _make_id("t")),
                "text": text.strip(),
                "ts": float(payload.get("ts") or time.time()),
            }
            if isinstance(payload.get("lang"), str) and payload.get("lang").strip():
                item["lang"] = payload.get("lang").strip()
            if isinstance(payload.get("voice"), str) and payload.get("voice").strip():
                item["voice"] = payload.get("voice").strip()
            if isinstance(payload.get("rate"), (int, float)):
                item["rate"] = float(payload.get("rate"))
            for ws in await _resolve_webspace_ids(payload):
                await _ensure_tts_state(ws)
                await _append_tts_queue_item(ws, item)

        def _call_voice_chat_tool(text: str, meta: dict) -> Any:
            ctx = get_ctx()
            skills_root = ctx.paths.skills_workspace_dir()
            skills_root = skills_root() if callable(skills_root) else skills_root
            skill_dir = Path(skills_root) / "voice_chat_skill"
            prev = ctx.skill_ctx.get()
            try:
                ctx.skill_ctx.set("voice_chat_skill", skill_dir)
                return execute_tool(
                    skill_dir,
                    module="handlers.main",
                    attr="handle_text",
                    payload={"text": text, "_meta": meta},
                )
            finally:
                if prev is None:
                    try:
                        ctx.skill_ctx.clear()
                    except Exception:
                        pass
                else:
                    try:
                        ctx.skill_ctx.set(prev.name, prev.path)
                    except Exception:
                        pass

        async def _on_voice_user(ev: Event) -> None:
            payload = ev.payload or {}
            target_webspaces = await _resolve_webspace_ids(payload)
            ws = target_webspaces[0] if target_webspaces else "default"
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                return
            text = text.strip()

            await _ensure_voice_chat_state(ws)

            meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
            meta = {**meta, "webspace_id": ws}
            if len(target_webspaces) > 1:
                meta["webspace_ids"] = list(target_webspaces)
            try:
                self.bus.publish(
                    Event(
                        type="io.out.chat.append",
                        source="router",
                        ts=time.time(),
                        payload={
                            "id": _make_id("m"),
                            "from": "user",
                            "text": text,
                            "ts": time.time(),
                            "_meta": meta,
                          },
                      )
                  )
            except Exception:
                pass
            # Fire-and-forget NLU detection so that text commands can be
            # mapped to scenario/skill actions via Rasa-based interpreter.
            try:
                self.bus.publish(
                    Event(
                        type="nlp.intent.detect",
                        source="router.voice",
                        ts=time.time(),
                        payload={"text": text, "webspace_id": ws},
                    )
                )
            except Exception:
                pass
            try:
                await _ensure_tts_state(ws)
            except Exception:
                pass
            try:
                await asyncio.to_thread(_call_voice_chat_tool, text, meta)
            except Exception as exc:
                # Do not crash the router on skill/tool failures; surface the error in chat.
                try:
                    msg = {
                        "id": _make_id("err"),
                        "from": "hub",
                        "text": f"Ошибка обработки: {exc}",
                        "ts": time.time(),
                    }
                    msg["text"] = f"Ошибка обработки: {exc}"
                    await _append_voice_chat_message(ws, msg)
                except Exception:
                    pass
                try:
                    logging.getLogger("adaos.router").warning("voice.chat.user failed", exc_info=True)
                except Exception:
                    pass

        self.bus.subscribe("voice.chat.open", _on_voice_open)
        self.bus.subscribe("voice.chat.user", _on_voice_user)
        self.bus.subscribe("io.out.chat.append", _on_io_out_chat_append)
        self.bus.subscribe("io.out.say", _on_io_out_say)

        # Watch rules file
        def _reload(rules: list[dict]):
            self._rules = rules or []

        # Preload rules and start watcher
        try:
            node_id = get_ctx().config.node_id
        except Exception:
            # fallback: do not crash router if config is not ready yet
            node_id = ""
        self._rules = load_rules(self.base_dir, node_id)
        self._stop_watch = watch_rules(self.base_dir, node_id, _reload)

    async def stop(self) -> None:
        if self._stop_watch:
            try:
                self._stop_watch()
            except Exception:
                pass
            self._stop_watch = None
        self._started = False
