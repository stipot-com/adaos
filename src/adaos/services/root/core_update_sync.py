from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

from adaos.services.agent_context import get_ctx
from adaos.services.core_slots import active_slot_manifest, slot_status
from adaos.services.core_update import read_status
from adaos.services.root.client import RootHttpClient


def _root_client(conf) -> RootHttpClient | None:
    try:
        ctx = get_ctx()
    except Exception:
        return None
    base_url = str(getattr(ctx.settings, "api_base", None) or getattr(getattr(conf, "root_settings", None), "base_url", None) or "").rstrip("/")
    if not base_url:
        return None
    cert_path = conf.hub_cert_path()
    key_path = conf.hub_key_path()
    ca_path = conf.ca_cert_path()
    if not cert_path.exists() or not key_path.exists():
        return None
    verify: str | bool = str(ca_path) if ca_path.exists() else True
    return RootHttpClient(base_url=base_url, verify=verify, cert=(str(cert_path), str(key_path)))


def build_core_update_report(conf) -> dict[str, Any]:
    return {
        "status": read_status(),
        "slot_status": slot_status(),
        "node_id": str(getattr(conf, "node_id", "") or ""),
        "subnet_id": str(getattr(conf, "subnet_id", "") or ""),
        "role": str(getattr(conf, "role", "") or ""),
    }


def report_hub_core_update_state(conf) -> dict[str, Any] | None:
    client = _root_client(conf)
    if client is None:
        return None
    payload = build_core_update_report(conf)
    payload["reported_at"] = time.time()
    return client.hub_core_update_report(payload=payload)


def reconcile_hub_core_update(conf, *, countdown_sec: float = 60.0) -> dict[str, Any] | None:
    client = _root_client(conf)
    if client is None:
        return None
    try:
        report_hub_core_update_state(conf)
    except Exception:
        logging.getLogger("adaos.hub-io").debug("core update state report failed", exc_info=True)
    manifest = active_slot_manifest() or {}
    branch = str(manifest.get("target_rev") or manifest.get("git_branch") or os.getenv("ADAOS_REV") or os.getenv("ADAOS_INIT_REV") or "").strip()
    current_commit = str(manifest.get("git_commit") or "").strip()
    release = client.hub_core_update_release(branch=branch or None, current_commit=current_commit or None)
    if not isinstance(release, dict) or not release.get("ok"):
        return release if isinstance(release, dict) else None
    if not bool(release.get("needs_update")):
        return release
    release_info = release.get("release") if isinstance(release.get("release"), dict) else {}
    target_rev = str(release_info.get("branch") or branch or "").strip()
    head_sha = str(release_info.get("head_sha") or "").strip()
    local_token = str(getattr(conf, "token", "") or os.getenv("ADAOS_TOKEN") or "").strip()
    if not local_token:
        raise RuntimeError("missing local ADAOS token for self-update reconcile")
    ctx = get_ctx()
    host = str(getattr(ctx.settings, "host", None) or "127.0.0.1").strip() or "127.0.0.1"
    port = int(getattr(ctx.settings, "port", None) or 8777)
    response = requests.post(
        f"http://{host}:{port}/api/admin/update/start",
        json={
            "target_rev": target_rev,
            "target_version": head_sha[:12] if head_sha else "",
            "reason": f"root.release:{target_rev}{(':' + head_sha[:12]) if head_sha else ''}",
            "countdown_sec": float(release_info.get("countdown_sec") or countdown_sec),
            "drain_timeout_sec": 10,
            "signal_delay_sec": 0.25,
        },
        headers={"X-AdaOS-Token": local_token},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    return {
        **release,
        "dispatch": payload,
    }


__all__ = [
    "build_core_update_report",
    "reconcile_hub_core_update",
    "report_hub_core_update_state",
]
