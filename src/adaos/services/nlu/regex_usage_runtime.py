from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from adaos.services.agent_context import get_ctx

_TTL_S = 2.0
_recent: dict[tuple[str, str], float] = {}


def _usage_path() -> Path:
    ctx = get_ctx()
    return Path(ctx.paths.state_dir()) / "nlu" / "regex_usage.jsonl"


def record_regex_rule_hit(
    *,
    webspace_id: str,
    scenario_id: str | None,
    rule_id: str,
    intent: str,
    pattern: str,
    text: str,
    slots: Mapping[str, Any] | None = None,
    raw: Mapping[str, Any] | None = None,
    via: str = "regex.dynamic",
) -> None:
    """
    Append a compact JSONL record for later analysis/optimization.
    """
    if not webspace_id or not rule_id:
        return

    now = time.time()
    key = (webspace_id, rule_id)
    last = _recent.get(key)
    if last is not None and now - last < _TTL_S:
        return
    _recent[key] = now

    path = _usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    rec = {
        "ts": now,
        "webspace_id": webspace_id,
        "scenario_id": scenario_id,
        "via": via,
        "rule_id": rule_id,
        "intent": intent,
        "pattern": pattern,
        "text": text,
        "slots": dict(slots or {}),
        "raw": dict(raw or {}),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

