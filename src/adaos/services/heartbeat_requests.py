from __future__ import annotations

import asyncio
import os
from typing import Sequence
from urllib.parse import urlparse

import requests

from adaos.ports.heartbeat import HeartbeatPort
from adaos.services.capacity import get_local_capacity


class RequestsHeartbeat(HeartbeatPort):
    """
    Heartbeat implementation via `requests`, safe for asyncio via `asyncio.to_thread`.

    Notes:
    - Disables environment proxies (`trust_env=False`) to avoid accidental proxying of LAN/localhost hub URLs.
    - Tolerates bare host:port by prefixing http://.
    """

    def __init__(self, timeout: float = 3.0) -> None:
        self.timeout = float(timeout)
        self._session = requests.Session()
        try:
            self._session.trust_env = False
        except Exception:
            pass

    @staticmethod
    def _normalize_base(hub_url: str) -> str | None:
        text = str(hub_url or "").strip()
        if not text:
            return None
        u = urlparse(text)
        if u.scheme in ("http", "https") and u.netloc:
            return text.rstrip("/")
        if not u.scheme and not u.netloc and u.path:
            # tolerate bare host:port (or host)
            return ("http://" + u.path).rstrip("/")
        return None

    async def register(self, hub_url: str, token: str, *, node_id: str, subnet_id: str, hostname: str, roles: Sequence[str]) -> bool:
        base = self._normalize_base(hub_url)
        if not base:
            return False
        url = f"{base}/api/subnet/register"
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
        try:
            r = await asyncio.to_thread(self._session.post, url, json=payload, headers=headers, timeout=self.timeout)
            return r.status_code == 200
        except Exception:
            return False

    async def heartbeat(self, hub_url: str, token: str, *, node_id: str) -> bool:
        base = self._normalize_base(hub_url)
        if not base:
            return False
        url = f"{base}/api/subnet/heartbeat"
        headers = {"X-AdaOS-Token": token}
        try:
            capacity = get_local_capacity()
        except Exception:
            capacity = None
        payload: dict = {"node_id": node_id}
        if capacity is not None:
            payload["capacity"] = capacity
        try:
            r = await asyncio.to_thread(self._session.post, url, json=payload, headers=headers, timeout=self.timeout)
            return r.status_code == 200
        except Exception:
            return False

    async def deregister(self, hub_url: str, token: str, *, node_id: str) -> None:
        base = self._normalize_base(hub_url)
        if not base:
            return
        url = f"{base}/api/subnet/deregister"
        headers = {"X-AdaOS-Token": token}
        payload = {"node_id": node_id}
        try:
            await asyncio.to_thread(self._session.post, url, json=payload, headers=headers, timeout=self.timeout)
        except Exception:
            pass

