from __future__ import annotations

import os
import time
from typing import Any, Mapping


_DEFAULT_AGING_AFTER_S = 30.0
_DEFAULT_STALE_AFTER_S = 90.0


def _env_float(name: str, default: float) -> float:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    return value if value > 0.0 else default


def subnet_runtime_projection_policy() -> dict[str, float]:
    aging_after_s = _env_float(
        "ADAOS_SUBNET_RUNTIME_PROJECTION_AGING_AFTER_S",
        _DEFAULT_AGING_AFTER_S,
    )
    stale_after_s = _env_float(
        "ADAOS_SUBNET_RUNTIME_PROJECTION_STALE_AFTER_S",
        _DEFAULT_STALE_AFTER_S,
    )
    if stale_after_s < aging_after_s:
        stale_after_s = aging_after_s
    return {
        "aging_after_s": aging_after_s,
        "stale_after_s": stale_after_s,
    }


def subnet_runtime_projection_freshness(
    projection: Mapping[str, Any] | None,
    *,
    online: bool | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    data = projection if isinstance(projection, Mapping) else {}
    snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), Mapping) else {}
    captured_at = data.get("captured_at")
    if captured_at is None:
        captured_at = snapshot.get("captured_at")
    try:
        captured_at_value = float(captured_at) if captured_at is not None else 0.0
    except Exception:
        captured_at_value = 0.0
    now_value = time.time() if now is None else float(now)
    policy = subnet_runtime_projection_policy()
    aging_after_s = float(policy.get("aging_after_s") or _DEFAULT_AGING_AFTER_S)
    stale_after_s = float(policy.get("stale_after_s") or _DEFAULT_STALE_AFTER_S)
    age_s = round(max(0.0, now_value - captured_at_value), 3) if captured_at_value > 0.0 else None

    if online is False:
        state = "stale"
        reason = "node_offline"
    elif captured_at_value <= 0.0:
        state = "pending"
        reason = "snapshot_missing"
    elif isinstance(age_s, (int, float)) and float(age_s) >= stale_after_s:
        state = "stale"
        reason = "snapshot_stale"
    elif isinstance(age_s, (int, float)) and float(age_s) >= aging_after_s:
        state = "aging"
        reason = "snapshot_aging"
    else:
        state = "fresh"
        reason = "snapshot_fresh"

    return {
        "state": state,
        "reason": reason,
        "online": online,
        "captured_at": captured_at_value if captured_at_value > 0.0 else None,
        "age_s": age_s,
        "aging_after_s": aging_after_s,
        "stale_after_s": stale_after_s,
    }


__all__ = [
    "subnet_runtime_projection_freshness",
    "subnet_runtime_projection_policy",
]
