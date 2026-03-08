from __future__ import annotations
import asyncio
import logging
import time
from collections import defaultdict
from threading import RLock
from typing import Callable, Awaitable, Any, DefaultDict, List

from adaos.domain import Event
from adaos.ports import EventBus


Handler = Callable[[Event], Any] | Callable[[Event], Awaitable[Any]]

_log = logging.getLogger("adaos.eventbus")


def _handler_label(handler: Handler) -> str:
    """
    Build a human-readable label for a handler, including optional skill/topic
    hints injected by the SDK decorators.
    """
    mod = getattr(handler, "__module__", None) or "<?>"
    name = getattr(handler, "__name__", None) or repr(handler)
    skill = getattr(handler, "_adaos_skill", None)
    topic = getattr(handler, "_adaos_topic", None)
    parts = [f"{mod}.{name}"]
    if skill:
        parts.append(f"skill={skill}")
    if topic:
        parts.append(f"topic={topic}")
    return " ".join(parts)


async def _run_coro_with_timing(coro: Awaitable[Any], handler: Handler, event: Event) -> None:
    """
    Wrapper for async handlers that records execution time and logs slow/crashing
    handlers for debugging high CPU usage in the hub.
    """
    started = time.perf_counter()
    try:
        await coro
    except Exception:  # pragma: no cover - defensive logging
        _log.warning(
            "event handler crashed handler=%s type=%s",
            _handler_label(handler),
            getattr(event, "type", "<unknown>"),
            exc_info=True,
        )
    else:
        duration = time.perf_counter() - started
        if duration >= 0.1:
            _log.warning(
                "slow async event handler handler=%s type=%s duration=%.3fs",
                _handler_label(handler),
                getattr(event, "type", "<unknown>"),
                duration,
            )


class LocalEventBus(EventBus):
    """
    Локальная неблокирующая шина событий для одного процесса.
      - subscribe(prefix, handler)
      - publish(event)

    Особенности:
      * prefix = "" или "*" — подписка на все события.
      * вызовы обработчиков делаются в текущем или уже запущенном event loop.

    Дополнительно эта реализация логирует медленные/падающие обработчики,
    чтобы упростить отладку случаев, когда какой‑то skill «крутит» CPU.
    """

    def __init__(self) -> None:
        self._subs: DefaultDict[str, List[Handler]] = defaultdict(list)
        self._lock = RLock()
        self._pending_tasks: set[asyncio.Task[Any]] = set()

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        with self._lock:
            self._pending_tasks.add(task)

        def _cleanup(done: asyncio.Task[Any]) -> None:
            with self._lock:
                self._pending_tasks.discard(done)

        task.add_done_callback(_cleanup)

    async def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """
        Wait until all async handlers spawned by ``publish()`` finish.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        while True:
            with self._lock:
                pending = [task for task in self._pending_tasks if not task.done()]
            if not pending:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.wait(pending, timeout=min(0.1, remaining), return_when=asyncio.FIRST_COMPLETED)

    def subscribe(self, type_prefix: str, handler: Handler) -> None:
        with self._lock:
            self._subs[type_prefix].append(handler)
        _log.debug("bus.subscribe prefix=%r handler=%s", type_prefix, _handler_label(handler))

    def publish(self, event: Event) -> None:
        with self._lock:
            pairs = [(p, hs[:]) for p, hs in self._subs.items()]

        if _log.isEnabledFor(logging.DEBUG):
            total_handlers = sum(
                len(hs) for p, hs in pairs if p == "" or p == "*" or event.type.startswith(p)
            )
            _log.debug(
                "bus.publish type=%s source=%s handlers=%d",
                getattr(event, "type", "<unknown>"),
                getattr(event, "source", "<unknown>"),
                total_handlers,
            )

        for prefix, handlers in pairs:
            if prefix != "*" and prefix != "" and not event.type.startswith(prefix):
                continue
            for h in handlers:
                started = time.perf_counter()
                try:
                    res = h(event)
                except Exception:  # pragma: no cover - defensive logging
                    _log.warning(
                        "event handler crashed handler=%s type=%s",
                        _handler_label(h),
                        getattr(event, "type", "<unknown>"),
                        exc_info=True,
                    )
                    continue

                if asyncio.iscoroutine(res):
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        # Если нет текущего цикла, fallback на asyncio.run (CLI/скрипты).
                        asyncio.run(res)
                    else:
                        task = loop.create_task(_run_coro_with_timing(res, h, event))
                        self._track_task(task)
                else:
                    duration = time.perf_counter() - started
                    if duration >= 0.05:
                        _log.warning(
                            "slow sync event handler handler=%s type=%s duration=%.3fs",
                            _handler_label(h),
                            getattr(event, "type", "<unknown>"),
                            duration,
                        )


def emit(bus: EventBus, type_: str, payload: dict, source: str) -> None:
    bus.publish(Event(type=type_, payload=payload, source=source, ts=time.time()))

