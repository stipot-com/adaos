from __future__ import annotations
import asyncio, os
import socket
from typing import Sequence
import requests

from adaos.ports.heartbeat import HeartbeatPort
from adaos.services.capacity import get_local_capacity


# Реализация через requests, но безопасно для event loop (to_thread)
class RequestsHeartbeat(HeartbeatPort):
    def __init__(self, timeout: float = 3.0) -> None:
        self.timeout = timeout

    async def register(self, hub_url: str, token: str, *, node_id: str, subnet_id: str, hostname: str, roles: Sequence[str]) -> bool:
        url = f"{hub_url.rstrip('/')}/api/subnet/register"
        headers = {"X-AdaOS-Token": token}
        base_url = os.environ.get("ADAOS_SELF_BASE_URL") or None
        try:
            capacity = get_local_capacity()
        except Exception:
            capacity = {"io": [{"io_type": "stdout", "capabilities": ["text"], "priority": 50}]}
        payload = {
            "node_id": node_id,
            "subnet_id": subnet_id,
            "hostname": hostname,
            "roles": list(roles),
            "base_url": base_url,
            "capacity": capacity,
        }
        r = await asyncio.to_thread(requests.post, url, json=payload, headers=headers, timeout=self.timeout)
        return r.status_code == 200

    async def heartbeat(self, hub_url: str, token: str, *, node_id: str) -> bool:
        url = f"{hub_url.rstrip('/')}/api/subnet/heartbeat"
        headers = {"X-AdaOS-Token": token}
        try:
            capacity = get_local_capacity()
        except Exception:
            capacity = None
        payload = {"node_id": node_id}
        if capacity is not None:
            payload["capacity"] = capacity
        r = await asyncio.to_thread(requests.post, url, json=payload, headers=headers, timeout=self.timeout)
        return r.status_code == 200

    async def deregister(self, hub_url: str, token: str, *, node_id: str) -> None:
        url = f"{hub_url.rstrip('/')}/api/subnet/deregister"
        headers = {"X-AdaOS-Token": token}
        payload = {"node_id": node_id}
        try:
            await asyncio.to_thread(requests.post, url, json=payload, headers=headers, timeout=self.timeout)
        except Exception:
            # без фейла — если хаб недоступен, просто продолжаем
            pass
