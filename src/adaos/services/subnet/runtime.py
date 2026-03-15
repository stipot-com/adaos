from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from adaos.services.agent_context import get_ctx
from adaos.services.subnet.link_client import get_member_link_client
from adaos.services.subnet.link_manager import get_hub_link_manager
from adaos.services.yjs.store import add_ystore_write_listener

_log = logging.getLogger("adaos.subnet.runtime")


async def start_subnet_p2p(app: Any) -> None:
    if getattr(app.state, "subnet_p2p", None):
        return

    conf = get_ctx().config
    yjs_enabled = os.getenv("ADAOS_SUBNET_YJS_REPLICATION", "1").strip().lower() not in ("0", "false", "no")

    state: dict[str, Any] = {"remove_ystore_listener": None}

    if conf.role == "hub" and yjs_enabled:
        mgr = get_hub_link_manager()

        def _on_write(webspace_id: str, update: bytes) -> None:
            if not update:
                return
            try:
                asyncio.get_running_loop().create_task(
                    mgr.broadcast_yjs_update(webspace_id=webspace_id or "default", update=update, origin_node_id=None)
                )
            except Exception:
                return

        state["remove_ystore_listener"] = add_ystore_write_listener(_on_write)

    if conf.role == "member":
        try:
            await get_member_link_client().start()
        except Exception:
            _log.debug("failed to start member P2P client", exc_info=True)

    app.state.subnet_p2p = state


async def stop_subnet_p2p(app: Any) -> None:
    state = getattr(app.state, "subnet_p2p", None)
    if not state:
        return
    try:
        rm = state.get("remove_ystore_listener")
        if rm:
            rm()
    except Exception:
        pass
    try:
        conf = get_ctx().config
        if conf.role == "member":
            await get_member_link_client().stop()
    except asyncio.CancelledError:
        # Expected during app shutdown.
        pass
    except Exception:
        pass
    try:
        app.state.subnet_p2p = None
    except Exception:
        pass
