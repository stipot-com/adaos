from __future__ import annotations

import logging
from typing import Callable, Dict, List, Set, Tuple

import y_py as Y

_log = logging.getLogger("adaos.yjs.observers")

RoomObserver = Callable[[str, Y.YDoc], None]

_OBSERVERS: List[RoomObserver] = []
_ATTACHED_OBSERVERS: Dict[Tuple[str, int], Set[RoomObserver]] = {}
_ACTIVE_YDOC_IDS: Dict[str, int] = {}


def _observer_name(observer: RoomObserver) -> str:
    try:
        return getattr(observer, "__name__", repr(observer))
    except Exception:  # pragma: no cover - defensive logging only
        return repr(observer)


def register_room_observer(observer: RoomObserver) -> None:
    """
    Register a callback that will be invoked for each Yjs room/YDoc.

    The observer receives ``(webspace_id, ydoc)`` and may attach its own
    ``observe_*`` hooks or perform one-off initialization. Errors are logged
    but do not affect other observers.
    """
    if observer in _OBSERVERS:
        return
    _OBSERVERS.append(observer)
    name = _observer_name(observer)
    _log.debug("room observer registered observer=%s total=%d", name, len(_OBSERVERS))


def list_room_observers() -> Tuple[RoomObserver, ...]:
    """
    Return a snapshot of registered room observers.
    """
    return tuple(_OBSERVERS)


def attach_room_observers(webspace_id: str, ydoc: Y.YDoc) -> None:
    """
    Invoke all registered room observers for the given webspace/YDoc.

    This is called by the Y gateway when a YRoom is created or reused.
    """
    ydoc_id = id(ydoc)
    previous_ydoc_id = _ACTIVE_YDOC_IDS.get(webspace_id)
    if previous_ydoc_id is not None and previous_ydoc_id != ydoc_id:
        _ATTACHED_OBSERVERS.pop((webspace_id, previous_ydoc_id), None)
    _ACTIVE_YDOC_IDS[webspace_id] = ydoc_id

    attached = _ATTACHED_OBSERVERS.setdefault((webspace_id, ydoc_id), set())
    for observer in list_room_observers():
        if observer in attached:
            continue
        try:
            observer(webspace_id, ydoc)
            attached.add(observer)
        except Exception:  # pragma: no cover - defensive logging only
            name = _observer_name(observer)
            _log.warning(
                "room observer failed observer=%s webspace=%s",
                name,
                webspace_id,
                exc_info=True,
            )


def forget_room_observers(webspace_id: str, ydoc: Y.YDoc | None = None) -> None:
    """
    Drop observer bookkeeping for a webspace/YDoc pair during room teardown.

    This does not unregister low-level y_py callbacks (there is no public API
    for that), but it prevents our own attachment registries from retaining
    stale room identities across repeated recreate/reset cycles.
    """
    key = str(webspace_id or "").strip() or "default"
    active_ydoc_id = _ACTIVE_YDOC_IDS.get(key)
    target_ydoc_id = id(ydoc) if ydoc is not None else active_ydoc_id
    if target_ydoc_id is not None:
        _ATTACHED_OBSERVERS.pop((key, target_ydoc_id), None)
    if active_ydoc_id is not None and target_ydoc_id == active_ydoc_id:
        _ACTIVE_YDOC_IDS.pop(key, None)

