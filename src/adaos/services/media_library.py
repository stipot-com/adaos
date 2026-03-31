from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from adaos.services.agent_context import get_ctx
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment


MEDIA_SKILL_NAME = "infrastate_skill"
MEDIA_STORAGE_SUBPATH = "data/files"
ROOT_ROUTED_MEDIA_BODY_LIMIT_BYTES = 2 * 1024 * 1024
MEDIA_RUNTIME_SCOPE = "hub_local_media_debug"
ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES = 512 * 1024 * 1024
ROOT_MEDIA_RELAY_CHUNK_BYTES = 256 * 1024
SUPPORTED_VIDEO_EXTENSIONS = {
    ".mp4",
    ".webm",
    ".ogv",
    ".ogg",
    ".mov",
    ".m4v",
    ".mkv",
    ".avi",
    ".wmv",
}
_MEDIA_TYPE_OVERRIDES = {
    ".mkv": "video/x-matroska",
    ".m4v": "video/mp4",
    ".ogv": "video/ogg",
    ".wmv": "video/x-ms-wmv",
    ".avi": "video/x-msvideo",
}


def media_runtime_env() -> SkillRuntimeEnvironment:
    ctx = get_ctx()
    env = SkillRuntimeEnvironment(
        skills_root=Path(ctx.paths.skills_dir()),
        skill_name=MEDIA_SKILL_NAME,
    )
    env.ensure_base()
    return env


def media_video_dir() -> Path:
    path = media_runtime_env().files_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_media_filename(filename: str) -> str:
    raw = str(filename or "").strip()
    if not raw:
        raise ValueError("empty_filename")
    if "\x00" in raw:
        raise ValueError("invalid_filename")
    if "/" in raw or "\\" in raw:
        raise ValueError("path_separators_not_allowed")
    if raw in {".", ".."}:
        raise ValueError("invalid_filename")
    name = Path(raw).name
    if name != raw:
        raise ValueError("path_traversal_not_allowed")
    suffix = Path(name).suffix.lower()
    if not suffix:
        raise ValueError("missing_extension")
    if suffix not in SUPPORTED_VIDEO_EXTENSIONS:
        raise ValueError(f"unsupported_extension:{suffix}")
    return name


def media_file_path(filename: str) -> Path:
    name = sanitize_media_filename(filename)
    return media_video_dir() / name


def guess_media_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in _MEDIA_TYPE_OVERRIDES:
        return _MEDIA_TYPE_OVERRIDES[suffix]
    guessed, _enc = mimetypes.guess_type(filename)
    if guessed:
        return guessed
    return "application/octet-stream"


def media_capabilities() -> dict[str, Any]:
    try:
        from adaos.services.webrtc.peer import webrtc_peer_snapshot

        live_webrtc = webrtc_peer_snapshot()
        webrtc_supported = True
    except Exception:
        live_webrtc = {}
        webrtc_supported = False
    return {
        "storage": {
            "dir": str(media_video_dir()),
            "subpath": MEDIA_STORAGE_SUBPATH,
        },
        "upload": {
            "direct_local": {
                "ready": True,
                "mode": "http_raw_put",
                "note": "Raw PUT upload is available when the browser talks to the local hub API directly.",
            },
            "root_routed": {
                "ready": True,
                "mode": "bounded_media_relay",
                "note": "Dedicated /hubs/<id>/media/* relay path supports bounded upload streaming via root.",
                "max_upload_bytes_hint": ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES,
            },
        },
        "playback": {
            "direct_local": {
                "ready": True,
                "mode": "http_file_response",
                "note": "Progressive file playback is available on the direct local hub API path.",
            },
            "root_routed": {
                "ready": True,
                "mode": "bounded_media_relay",
                "note": "Dedicated /hubs/<id>/media/* relay path supports ranged playback via root.",
                "range_requests": True,
                "chunk_bytes_hint": ROOT_MEDIA_RELAY_CHUNK_BYTES,
            },
        },
        "broadcast": {
            "ready": bool(webrtc_supported),
            "mode": "webrtc_av_loopback" if webrtc_supported else "unavailable",
            "reason": "hub_webrtc_peer_loopback" if webrtc_supported else "webrtc_runtime_unavailable",
            "details": (
                "Browser camera/microphone tracks can be published to the hub and looped back for end-to-end media validation."
                if webrtc_supported
                else "Current runtime cannot load aiortc media support."
            ),
            "peer_total": int(live_webrtc.get("peer_total") or 0),
            "connected_peers": int(live_webrtc.get("connected_peers") or 0),
            "incoming_audio_tracks": int(live_webrtc.get("incoming_audio_tracks") or 0),
            "incoming_video_tracks": int(live_webrtc.get("incoming_video_tracks") or 0),
            "loopback_audio_tracks": int(live_webrtc.get("loopback_audio_tracks") or 0),
            "loopback_video_tracks": int(live_webrtc.get("loopback_video_tracks") or 0),
        },
        "notes": [
            "Direct local hub API remains the preferred path for operator-grade upload and playback validation.",
            "Root-routed media now uses a dedicated bounded relay path instead of the generic buffered JSON /api proxy.",
            "WebRTC audio/video loopback is available for live end-to-end media channel validation.",
        ],
    }


def media_runtime_snapshot(items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    items = list(items) if isinstance(items, list) else list_media_files()
    total_bytes = sum(int(item.get("size_bytes") or 0) for item in items)
    try:
        from adaos.services.webrtc.peer import webrtc_peer_snapshot

        live_webrtc = webrtc_peer_snapshot()
        webrtc_supported = True
    except Exception:
        live_webrtc = {}
        webrtc_supported = False
    return {
        "available": True,
        "scope": MEDIA_RUNTIME_SCOPE,
        "authority": {
            "storage": "local_hub_api",
            "playback": "local_hub_api",
            "relay": "root_media_relay",
            "broadcast": "hub_webrtc_peer_loopback" if webrtc_supported else "unavailable",
        },
        "assessment": {
            "state": "relay_and_webrtc_media_available" if webrtc_supported else "bounded_relay_available",
            "reason": (
                "media plane supports direct-local authority, bounded root relay authority, and live WebRTC audio/video loopback"
                if webrtc_supported
                else "media plane supports direct-local authority and bounded root relay authority on a dedicated path"
            ),
        },
        "paths": {
            "direct_local_http": {
                "ready": True,
                "upload": True,
                "playback": "full",
                "authority": "local_hub_api",
                "mode": "http_raw_put + http_file_response",
            },
            "root_routed_http": {
                "ready": True,
                "upload": True,
                "playback": "full",
                "authority": "root_media_relay",
                "mode": "bounded_media_relay",
                "reason": "root_media_relay_streams_upload_and_playback_on_a_dedicated_path",
                "max_upload_bytes_hint": ROOT_MEDIA_RELAY_MAX_UPLOAD_BYTES,
                "chunk_bytes_hint": ROOT_MEDIA_RELAY_CHUNK_BYTES,
            },
            "webrtc_tracks": {
                "ready": bool(webrtc_supported),
                "upload": False,
                "playback": "live_loopback" if webrtc_supported else "not_supported",
                "authority": "hub_webrtc_peer_loopback" if webrtc_supported else "none",
                "mode": "webrtc_audio_video_tracks" if webrtc_supported else "not_implemented",
                "reason": "hub_webrtc_peer_loopback" if webrtc_supported else "webrtc_media_tracks_not_implemented",
                "peer_total": int(live_webrtc.get("peer_total") or 0),
                "connected_peers": int(live_webrtc.get("connected_peers") or 0),
                "incoming_audio_tracks": int(live_webrtc.get("incoming_audio_tracks") or 0),
                "incoming_video_tracks": int(live_webrtc.get("incoming_video_tracks") or 0),
                "loopback_audio_tracks": int(live_webrtc.get("loopback_audio_tracks") or 0),
                "loopback_video_tracks": int(live_webrtc.get("loopback_video_tracks") or 0),
            },
        },
        "recommended_path": "direct_local_http",
        "counts": {
            "file_total": len(items),
            "total_bytes": total_bytes,
            "live_peer_total": int(live_webrtc.get("peer_total") or 0),
            "live_connected_peers": int(live_webrtc.get("connected_peers") or 0),
            "incoming_audio_tracks": int(live_webrtc.get("incoming_audio_tracks") or 0),
            "incoming_video_tracks": int(live_webrtc.get("incoming_video_tracks") or 0),
            "loopback_audio_tracks": int(live_webrtc.get("loopback_audio_tracks") or 0),
            "loopback_video_tracks": int(live_webrtc.get("loopback_video_tracks") or 0),
        },
        "live_webrtc": live_webrtc if isinstance(live_webrtc, dict) else {},
        "storage": {
            "dir": str(media_video_dir()),
            "subpath": MEDIA_STORAGE_SUBPATH,
        },
        "notes": [
            "Direct local hub API remains the preferred path for real upload and playback validation.",
            "Root-routed media now uses a dedicated bounded relay path instead of the generic buffered /api proxy.",
            "WebRTC audio/video loopback is available for live end-to-end media validation against the hub.",
        ],
    }


def list_media_files() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    root = media_video_dir()
    for path in root.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            continue
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        items.append(
            {
                "name": path.name,
                "size_bytes": int(stat.st_size),
                "mime_type": guess_media_type(path.name),
                "modified_at": modified.isoformat(),
                "content_path": f"/api/node/media/files/content/{quote(path.name)}",
            }
        )
    items.sort(key=lambda item: (str(item.get("modified_at") or ""), str(item.get("name") or "")), reverse=True)
    return items


def media_snapshot() -> dict[str, Any]:
    items = list_media_files()
    total_bytes = sum(int(item.get("size_bytes") or 0) for item in items)
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "total_bytes": total_bytes,
        "capabilities": media_capabilities(),
        "runtime": media_runtime_snapshot(items),
    }
