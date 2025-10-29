from __future__ import annotations

from typing import Any, Callable
from pathlib import Path
import asyncio
import json
import requests
import os

from adaos.services.eventbus import LocalEventBus
import logging
from adaos.domain import Event
from adaos.services.node_config import load_config
from .rules_loader import load_rules, watch_rules
from adaos.services.registry.subnet_directory import get_directory
from adaos.services.io_console import print_text
from adaos.sdk.data.env import get_tts_backend
from adaos.adapters.audio.tts.native_tts import NativeTTS
from adaos.integrations.ovos.tts import OVOSTTSAdapter
from adaos.integrations.rhasspy.tts import RhasspyTTSAdapter


class RouterService:
    def __init__(self, eventbus: LocalEventBus, base_dir: Path) -> None:
        self.bus = eventbus
        self.base_dir = base_dir
        self._started = False
        self._stop_watch: Callable[[], None] | None = None
        self._rules: list[dict[str, Any]] = []
        self._subscribed = False

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

    def _on_event(self, ev: Event) -> None:
        payload = ev.payload or {}
        text = (payload or {}).get("text")
        if not isinstance(text, str) or not text:
            return

        conf = load_config()
        this_node = conf.node_id
        target_node = self._pick_target_node("stdout", this_node)

        if target_node == this_node:
            print_text(text, node_id=this_node, origin={"source": ev.source})
            return

        # Cross-node delivery: resolve base_url and POST
        base_url = self._resolve_node_base_url(target_node, conf.role, conf.hub_url)
        if not base_url:
            # Try fallback to any online node with stdout capability (hub only)
            if conf.role == "hub":
                try:
                    directory = get_directory()
                    candidates = []
                    for n in directory.list_known_nodes():
                        if not n.get("online"):
                            continue
                        for io in (n.get("capacity") or {}).get("io", []):
                            if (io.get("io_type") == "stdout"):
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
            if not base_url:
                try:
                    logging.getLogger("adaos.router").warning(f"router: target {target_node} offline/unresolved; fallback to local print")
                except Exception:
                    pass
                print_text(text, node_id=this_node, origin={"source": ev.source})
                return
        url = f"{base_url.rstrip('/')}/api/io/console/print"
        headers = {"X-AdaOS-Token": conf.token or "dev-local-token", "Content-Type": "application/json"}
        body = {"text": text, "origin": {"source": ev.source, "from": this_node}}
        try:
            requests.post(url, json=body, headers=headers, timeout=2.5)
        except Exception:
            pass

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
            token = (load_config().token or "dev-local-token")
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
            def _on_say(ev: Event) -> None:
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
                    adapter = NativeTTS() if mode == "native" else (OVOSTTSAdapter() if mode == "ovos" else RhasspyTTSAdapter())
                    adapter.say(text)
                except Exception:
                    print_text(text, node_id=this_node, origin={"source": ev.source})

            self.bus.subscribe("ui.say", _on_say)
            self._subscribed = True
        # Watch rules file
        def _reload(rules: list[dict]):
            self._rules = rules or []

        # Preload rules and start watcher
        self._rules = load_rules(self.base_dir, load_config().node_id)
        self._stop_watch = watch_rules(self.base_dir, load_config().node_id, _reload)

    async def stop(self) -> None:
        if self._stop_watch:
            try:
                self._stop_watch()
            except Exception:
                pass
            self._stop_watch = None
        self._started = False
