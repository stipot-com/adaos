# tests/test_proc_manager.py
from __future__ import annotations
import sys, asyncio
from adaos.services.agent_context import get_ctx
from adaos.domain.types import ProcessSpec


async def _sleepy():
    await asyncio.sleep(0.05)


async def _run_cmd_and_check():
    ctx = get_ctx()
    cmd = [sys.executable, "-c", "import time; time.sleep(0.05)"]
    h = await ctx.proc.start(ProcessSpec(name="cmd", cmd=cmd))
    st1 = await ctx.proc.status(h)
    assert st1 in ("running", "stopped")  # может быстро завершиться
    st2 = "running"
    for _ in range(40):  # up to ~2s on slow/loaded CI
        await asyncio.sleep(0.05)
        st2 = await ctx.proc.status(h)
        if st2 in ("stopped", "error"):
            break
    assert st2 in ("stopped", "error")


async def _run_coro_and_check():
    ctx = get_ctx()
    h = await ctx.proc.start(ProcessSpec(name="coro", entrypoint=_sleepy))
    st1 = await ctx.proc.status(h)
    assert st1 in ("running", "stopped")
    st2 = "running"
    for _ in range(40):  # up to ~2s on slow/loaded CI
        await asyncio.sleep(0.05)
        st2 = await ctx.proc.status(h)
        if st2 in ("stopped", "error"):
            break
    assert st2 in ("stopped", "error")


def test_proc_flows(event_loop):
    event_loop.run_until_complete(_run_cmd_and_check())
    event_loop.run_until_complete(_run_coro_and_check())
