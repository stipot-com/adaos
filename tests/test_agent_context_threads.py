from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def test_get_ctx_works_in_worker_thread() -> None:
    """
    FastAPI executes sync dependencies in a worker threadpool. ContextVars do not
    propagate there by default, so get_ctx() must have a process-wide fallback.
    """
    from adaos.services.agent_context import clear_ctx, get_ctx
    from adaos.services.testing.bootstrap import bootstrap_test_ctx

    tmp = Path(tempfile.mkdtemp())
    base_dir = tmp / ".adaos"
    slot = base_dir / "skill-slot"
    slot.mkdir(parents=True, exist_ok=True)

    handle = bootstrap_test_ctx(skill_name="demo", skill_slot_dir=slot, secrets={})
    try:
        main_ctx = get_ctx()
        with ThreadPoolExecutor(max_workers=1) as pool:
            other = pool.submit(get_ctx).result(timeout=3.0)
        assert other is main_ctx
    finally:
        clear_ctx()

