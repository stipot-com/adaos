# src\adaos\services\chat_io\telemetry.py
from __future__ import annotations

"""Telemetry helpers for chat IO (MVP in-memory counters)."""

from typing import Mapping, Any, Dict, Tuple

_COUNTERS: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}


def record_event(metric: str, labels: Mapping[str, str] | None = None, value: float = 1.0) -> None:
    key = (metric, tuple(sorted((labels or {}).items())))
    _COUNTERS[key] = _COUNTERS.get(key, 0.0) + float(value)


def snapshot() -> Dict[str, float]:
    out: Dict[str, float] = {}
    for (metric, labels), val in _COUNTERS.items():
        if not labels:
            out[metric] = val
        else:
            label_str = ",".join(f"{k}={v}" for k, v in labels)
            out[f"{metric}{{{label_str}}}"] = val
    return out
