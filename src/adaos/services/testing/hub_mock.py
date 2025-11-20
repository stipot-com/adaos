from __future__ import annotations
import asyncio
import json
import os
from typing import Any

# NATS bus on HUB has been removed. This test helper is deprecated.


async def run() -> None:
    msg = (
        "hub_mock deprecated: NATS was migrated to backend/root. "
        "HUB no longer hosts NATS subjects. Use backend integration or HTTP fallback."
    )
    print(msg)
    # keep coroutine signature but exit fast
    return None


if __name__ == "__main__":
    asyncio.run(run())
