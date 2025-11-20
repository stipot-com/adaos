from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import threading
import time
import yaml


def load_rules(base_dir: Path, this_node_id: str) -> list[dict[str, Any]]:
    path = Path(base_dir) / "route_rules.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return []

    items: list[dict[str, Any]] = []

    # Support full schema with rules: [ ... ]
    rules = data.get("rules") if isinstance(data, dict) else None
    if isinstance(rules, list) and rules:
        for r in rules:
            if isinstance(r, dict):
                prio = r.get("priority")
                try:
                    prio = int(prio)
                except Exception:
                    prio = 0
                items.append({**r, "priority": prio})
    else:
        # Minimal schema fallback: allow top-level node_id/kind/io_type/priority
        if isinstance(data, dict):
            node_id = data.get("node_id") or data.get("target")
            if isinstance(node_id, dict):
                # If 'target' provided as dict
                target = node_id
            else:
                target = {
                    "node_id": node_id or "this",
                    "kind": data.get("kind") or "io_type",
                    "io_type": data.get("io_type") or "stdout",
                }
            if target:
                prio_raw = data.get("priority")
                try:
                    prio = int(prio_raw) if prio_raw is not None else 50
                except Exception:
                    prio = 50
                items.append({"match": {}, "target": target, "priority": prio})
    # sort by priority desc
    items.sort(key=lambda x: int(x.get("priority") or 0), reverse=True)
    return items


def watch_rules(base_dir: Path, this_node_id: str, on_reload: Callable[[list[dict]], None]) -> Callable[[], None]:
    """Polls route_rules.yaml for changes with a small debounce and invokes on_reload.
    Returns a callable which stops the watcher.
    """
    path = Path(base_dir) / "route_rules.yaml"
    stop = threading.Event()
    lock = threading.Lock()
    last_mtime: float = 0.0

    def _worker():
        nonlocal last_mtime
        # Initial preload
        try:
            rules = load_rules(base_dir, this_node_id)
            on_reload(rules)
        except Exception:
            pass
        while not stop.is_set():
            try:
                if path.exists():
                    mtime = path.stat().st_mtime
                    if mtime > last_mtime:
                        last_mtime = mtime
                        # Debounce ~400ms
                        time.sleep(0.4)
                        rules = load_rules(base_dir, this_node_id)
                        on_reload(rules)
            except Exception:
                # swallow and continue
                pass
            stop.wait(1.0)

    t = threading.Thread(target=_worker, name="route-rules-watcher", daemon=True)
    t.start()

    def _stop():
        stop.set()
        t.join(timeout=1.0)

    return _stop
