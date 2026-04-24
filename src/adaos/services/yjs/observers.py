from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Tuple

import y_py as Y

_log = logging.getLogger("adaos.yjs.observers")

RoomObserverDetach = Callable[[], Any]
RoomObserver = Callable[[str, Y.YDoc], RoomObserverDetach | None]

_OBSERVERS: List[RoomObserver] = []
_ATTACHED_OBSERVERS: Dict[Tuple[str, int], Dict[RoomObserver, RoomObserverDetach | None]] = {}
_ACTIVE_YDOC_IDS: Dict[str, int] = {}


def _ensure_builtin_room_observers_loaded() -> None:
    try:
        import adaos.services.yjs.load_mark  # noqa: F401
    except Exception:
        pass


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


def _detach_attached_observers(key: Tuple[str, int]) -> None:
    attached = _ATTACHED_OBSERVERS.pop(key, None)
    if not attached:
        return
    webspace_id, _ydoc_id = key
    for observer, detach in list(attached.items()):
        if not callable(detach):
            continue
        try:
            detach()
        except Exception:  # pragma: no cover - defensive logging only
            name = _observer_name(observer)
            _log.warning(
                "room observer detach failed observer=%s webspace=%s",
                name,
                webspace_id,
                exc_info=True,
            )


def attach_room_observers(webspace_id: str, ydoc: Y.YDoc) -> None:
    """
    Invoke all registered room observers for the given webspace/YDoc.

    This is called by the Y gateway when a YRoom is created or reused.
    """
    _ensure_builtin_room_observers_loaded()
    ydoc_id = id(ydoc)
    previous_ydoc_id = _ACTIVE_YDOC_IDS.get(webspace_id)
    if previous_ydoc_id is not None and previous_ydoc_id != ydoc_id:
        _detach_attached_observers((webspace_id, previous_ydoc_id))
    _ACTIVE_YDOC_IDS[webspace_id] = ydoc_id

    attached = _ATTACHED_OBSERVERS.setdefault((webspace_id, ydoc_id), {})
    for observer in list_room_observers():
        if observer in attached:
            continue
        try:
            detach = observer(webspace_id, ydoc)
            attached[observer] = detach if callable(detach) else None
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

    When observers returned an explicit detach callback during attachment, it
    is invoked before the bookkeeping entry is removed. This lets room-bound
    observers unregister low-level Yjs callbacks where the runtime supports it.
    """
    key = str(webspace_id or "").strip() or "default"
    active_ydoc_id = _ACTIVE_YDOC_IDS.get(key)
    target_ydoc_id = id(ydoc) if ydoc is not None else active_ydoc_id
    if target_ydoc_id is not None:
        _detach_attached_observers((key, target_ydoc_id))
    if active_ydoc_id is not None and target_ydoc_id == active_ydoc_id:
        _ACTIVE_YDOC_IDS.pop(key, None)

