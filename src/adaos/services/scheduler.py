from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict

from adaos.sdk.data.bus import emit as bus_emit

_log = logging.getLogger("adaos.scheduler")


@dataclass
class Job:
    name: str
    topic: str
    interval: float
    payload: dict = field(default_factory=dict)
    enabled: bool = True
    next_run: float = field(default_factory=lambda: time.time())


class Scheduler:
    """
    Minimal in-process scheduler:
      * jobs are kept in-memory only (MVP);
      * on each tick, emits an event to the core bus instead of calling code.

    This keeps the execution model uniform with skills: everything reacts to
    events such as `sys.ystore.backup` rather than being invoked directly.
    """

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._stopped.set()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="adaos-scheduler")
        _log.info("scheduler started")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task and not self._task.done():
            self._task.cancel()

    async def ensure_every(self, name: str, interval: float, topic: str, payload: dict | None = None) -> Job:
        """
        Create or update a simple \"every N seconds\" job.
        """
        interval = float(interval)
        now = time.time()
        async with self._lock:
            job = self._jobs.get(name)
            if job is None:
                job = Job(
                    name=name,
                    topic=topic,
                    interval=interval,
                    payload=dict(payload or {}),
                    next_run=now + interval,
                )
                self._jobs[name] = job
                _log.info("scheduler job created name=%s topic=%s interval=%ss", name, topic, interval)
            else:
                job.topic = topic
                job.interval = interval
                job.payload = dict(payload or {})
                if job.next_run < now:
                    job.next_run = now + interval
                _log.info("scheduler job updated name=%s topic=%s interval=%ss", name, topic, interval)
            return job

    async def delete(self, name: str) -> None:
        async with self._lock:
            if self._jobs.pop(name, None) is not None:
                _log.info("scheduler job deleted name=%s", name)

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                async with self._lock:
                    jobs = [j for j in self._jobs.values() if j.enabled]

                if not jobs:
                    await asyncio.sleep(0.5)
                    continue

                now = time.time()
                due = [j for j in jobs if j.next_run <= now]
                if not due:
                    sleep_for = max(0.1, min(j.next_run for j in jobs) - now)
                    await asyncio.sleep(sleep_for)
                    continue

                for job in due:
                    job.next_run = now + job.interval
                    asyncio.create_task(self._fire(job), name=f"adaos-scheduler-job-{job.name}")
                await asyncio.sleep(0)  # yield control
        except asyncio.CancelledError:  # pragma: no cover - controlled shutdown
            pass
        except Exception:  # pragma: no cover - defensive logging
            _log.warning("scheduler loop crashed", exc_info=True)
        finally:
            _log.info("scheduler stopped")

    async def _fire(self, job: Job) -> None:
        try:
            await bus_emit(job.topic, job.payload, source="scheduler", job_name=job.name)
        except Exception:  # pragma: no cover - defensive logging
            _log.warning("scheduler job failed name=%s topic=%s", job.name, job.topic, exc_info=True)


_SCHEDULER: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _SCHEDULER
    if _SCHEDULER is None:
        _SCHEDULER = Scheduler()
    return _SCHEDULER


async def start_scheduler() -> None:
    """
    Public entrypoint used from bootstrap to start the background loop.
    """
    await get_scheduler().start()

