from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(slots=True)
class RuntimeLifecycleState:
    node_state: str = "ready"
    reason: str = ""
    changed_at: float = 0.0


_LOCK = threading.RLock()
_STATE = RuntimeLifecycleState(changed_at=time.time())


def reset_runtime_lifecycle() -> None:
    with _LOCK:
        _STATE.node_state = "ready"
        _STATE.reason = ""
        _STATE.changed_at = time.time()


def request_drain(*, reason: str) -> RuntimeLifecycleState:
    with _LOCK:
        if _STATE.node_state != "draining":
            _STATE.node_state = "draining"
            _STATE.reason = str(reason or "admin.drain")
            _STATE.changed_at = time.time()
        return RuntimeLifecycleState(
            node_state=_STATE.node_state,
            reason=_STATE.reason,
            changed_at=_STATE.changed_at,
        )


def runtime_lifecycle_snapshot() -> dict[str, object]:
    with _LOCK:
        return {
            "node_state": _STATE.node_state,
            "reason": _STATE.reason,
            "changed_at": _STATE.changed_at,
            "draining": _STATE.node_state == "draining",
            "accepting_new_work": _STATE.node_state == "ready",
        }


def runtime_node_state() -> str:
    with _LOCK:
        return _STATE.node_state


def is_draining() -> bool:
    return runtime_node_state() == "draining"


def is_accepting_new_work() -> bool:
    return not is_draining()
