from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from copy import deepcopy
import yaml

from adaos.services.eventbus import emit
from adaos.services.runtime_paths import current_base_dir


_CAPACITY_CACHE: dict[str, tuple[int | None, Dict[str, Any]]] = {}


def _cache_key(base_dir: Path) -> str:
    return str(Path(base_dir).expanduser().resolve())


def _node_yaml_mtime_ns(path: Path) -> int | None:
    try:
        if not path.exists():
            return None
        return int(path.stat().st_mtime_ns)
    except Exception:
        return None


def load_capacity_from_node_yaml(base_dir: Path | None = None) -> Dict[str, Any]:
    """
    Reads the legacy `capacity` section from node.yaml and returns a minimal snapshot.
    This is a migration/compatibility fallback; the durable source of truth is the
    local registry-backed capacity projection.

    Structure example:
    {
      "io": [
        {"io_type": "stdout", "capabilities": ["text", "lang:ru", "lang:en"], "priority": 50}
      ]
    }
    """
    # Resolve .adaos base dir lazily from env/context if not provided
    if base_dir is None:
        base_dir = current_base_dir()
    base_dir = Path(base_dir).expanduser().resolve()

    node_path = Path(base_dir) / "node.yaml"
    cache_key = _cache_key(base_dir)
    mtime_ns = _node_yaml_mtime_ns(node_path)
    cached = _CAPACITY_CACHE.get(cache_key)
    if cached and cached[0] == mtime_ns:
        return deepcopy(cached[1])
    try:
        data = yaml.safe_load(node_path.read_text(encoding="utf-8")) if node_path.exists() else {}
    except Exception:
        data = {}

    # Read capacity section: io + skills
    io_list: list[dict[str, Any]] = []
    skills_list: list[dict[str, Any]] = []
    scenarios_list: list[dict[str, Any]] = []
    io_cfg = (data or {}).get("capacity") or {}
    raw = io_cfg.get("io") if isinstance(io_cfg, dict) else None
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                io_list.append({
                    "io_type": item.get("io_type") or item.get("type") or "stdout",
                    "capabilities": list(item.get("capabilities") or []),
                    "priority": int(item.get("priority") or 50),
                    "id_hint": str(item.get("id_hint") or ""),
                })
    raw_sk = io_cfg.get("skills") if isinstance(io_cfg, dict) else None
    if isinstance(raw_sk, list):
        for s in raw_sk:
            if isinstance(s, dict):
                name = s.get("name") or s.get("id")
                if not name:
                    continue
                skills_list.append({
                    "name": str(name),
                    "version": str(s.get("version") or "unknown"),
                    "active": bool(s.get("active", True)),
                    "dev": bool(s.get("dev", False)),
                })
    raw_sc = io_cfg.get("scenarios") if isinstance(io_cfg, dict) else None
    if isinstance(raw_sc, list):
        for s in raw_sc:
            if isinstance(s, dict):
                name = s.get("name") or s.get("id")
                if not name:
                    continue
                scenarios_list.append({
                    "name": str(name),
                    "version": str(s.get("version") or "unknown"),
                    "active": bool(s.get("active", True)),
                    "dev": bool(s.get("dev", False)),
                })

    if not any(x.get("io_type") == "stdout" for x in io_list):
        io_list.append({
            "io_type": "stdout",
            "capabilities": ["text", "lang:ru", "lang:en"],
            "priority": 50,
        })
    if not any(x.get("io_type") == "say" for x in io_list):
        io_list.append({
            "io_type": "say",
            "capabilities": ["text", "lang:ru", "lang:en"],
            "priority": 40,
        })

    result = {"io": io_list, "skills": skills_list, "scenarios": scenarios_list}
    _CAPACITY_CACHE[cache_key] = (mtime_ns, deepcopy(result))
    return deepcopy(result)


def _normalize_capacity_snapshot(payload: Dict[str, Any] | None) -> Dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    io_list = [dict(item) for item in list(raw.get("io") or []) if isinstance(item, dict)]
    skills_list = [dict(item) for item in list(raw.get("skills") or []) if isinstance(item, dict)]
    scenarios_list = [dict(item) for item in list(raw.get("scenarios") or []) if isinstance(item, dict)]
    if not any(x.get("io_type") == "stdout" for x in io_list):
        io_list.append({
            "io_type": "stdout",
            "capabilities": ["text", "lang:ru", "lang:en"],
            "priority": 50,
        })
    if not any(x.get("io_type") == "say" for x in io_list):
        io_list.append({
            "io_type": "say",
            "capabilities": ["text", "lang:ru", "lang:en"],
            "priority": 40,
        })
    return {"io": io_list, "skills": skills_list, "scenarios": scenarios_list}


def _local_capacity_from_registry() -> Dict[str, Any] | None:
    try:
        from adaos.services.node_config import load_config
        from adaos.services.registry.subnet_directory import get_directory

        conf = load_config()
        node_id = str(getattr(conf, "node_id", "") or "").strip()
        if not node_id:
            return None
        repo = get_directory().repo
        return {
            "io": repo.io_for_node(node_id),
            "skills": repo.skills_for_node(node_id),
            "scenarios": repo.scenarios_for_node(node_id),
        }
    except Exception:
        return None


def _save_local_capacity_to_registry(payload: Dict[str, Any]) -> None:
    from adaos.services.node_config import load_config
    from adaos.services.registry.subnet_directory import get_directory

    conf = load_config()
    node_id = str(getattr(conf, "node_id", "") or "").strip()
    if not node_id:
        raise RuntimeError("node_id is not configured; cannot persist local capacity")
    repo = get_directory().repo
    snapshot = _normalize_capacity_snapshot(payload)
    repo.replace_io_capacity(node_id, snapshot.get("io") or [])
    repo.replace_skill_capacity(node_id, snapshot.get("skills") or [])
    repo.replace_scenario_capacity(node_id, snapshot.get("scenarios") or [])


def _clear_legacy_capacity_from_node_yaml(*, base_dir: Path | None = None) -> None:
    data = _load_node_yaml(base_dir)
    if not isinstance(data, dict) or "capacity" not in data:
        return
    next_payload = dict(data)
    next_payload.pop("capacity", None)
    _save_node_yaml(next_payload, base_dir)


def _seed_local_capacity_from_legacy_if_needed(*, base_dir: Path | None = None) -> Dict[str, Any] | None:
    registry_snapshot = _local_capacity_from_registry()
    if registry_snapshot is None:
        return None
    if any(list(registry_snapshot.get(key) or []) for key in ("io", "skills", "scenarios")):
        _clear_legacy_capacity_from_node_yaml(base_dir=base_dir)
        return _normalize_capacity_snapshot(registry_snapshot)
    legacy = load_capacity_from_node_yaml(base_dir)
    try:
        _save_local_capacity_to_registry(legacy)
        _clear_legacy_capacity_from_node_yaml(base_dir=base_dir)
    except Exception:
        return _normalize_capacity_snapshot(legacy)
    registry_snapshot = _local_capacity_from_registry()
    if registry_snapshot is not None:
        return _normalize_capacity_snapshot(registry_snapshot)
    return _normalize_capacity_snapshot(legacy)


def get_local_capacity() -> Dict[str, Any]:
    snapshot = _seed_local_capacity_from_legacy_if_needed()
    if snapshot is not None:
        return snapshot
    return _normalize_capacity_snapshot(load_capacity_from_node_yaml())


# ----- legacy node.yaml compatibility helpers -----

def _resolve_base_dir(base_dir: Path | None = None) -> Path:
    if base_dir is not None:
        return base_dir
    return current_base_dir()


def _load_node_yaml(base_dir: Path | None = None) -> Dict[str, Any]:
    base = _resolve_base_dir(base_dir)
    path = Path(base) / "node.yaml"
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_node_yaml(data: Dict[str, Any], base_dir: Path | None = None) -> None:
    base = _resolve_base_dir(base_dir)
    path = Path(base) / "node.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _CAPACITY_CACHE.pop(_cache_key(base), None)


def _get_ctx_for_events():
    from adaos.services.agent_context import get_ctx

    return get_ctx()


def _emit_capacity_changed(*, base_dir: Path | None = None) -> None:
    try:
        ctx = _get_ctx_for_events()
        emit(
            ctx.bus,
            "capacity.changed",
            {
                "base_dir": str(_resolve_base_dir(base_dir)),
                "capacity": get_local_capacity(),
            },
            "capacity",
        )
    except Exception:
        pass


def _mutate_local_capacity(mutator, *, base_dir: Path | None = None) -> bool:
    registry_snapshot = _local_capacity_from_registry()
    if registry_snapshot is None:
        return False
    payload = _seed_local_capacity_from_legacy_if_needed(base_dir=base_dir) or _normalize_capacity_snapshot(registry_snapshot)
    mutator(payload)
    _save_local_capacity_to_registry(payload)
    _clear_legacy_capacity_from_node_yaml(base_dir=base_dir)
    return True


def install_skill_in_capacity(name: str, version: str, *, active: bool = True, dev: bool = False, base_dir: Path | None = None) -> None:
    if _mutate_local_capacity(
        lambda payload: _install_skill_record(payload, name, version, active=active, dev=dev),
        base_dir=base_dir,
    ):
        _emit_capacity_changed(base_dir=base_dir)
        return
    data = _load_node_yaml(base_dir)
    cap = data.setdefault("capacity", {})
    skills: List[Dict[str, Any]] = cap.setdefault("skills", [])  # type: ignore[assignment]
    # replace by name
    found = False
    for s in skills:
        if isinstance(s, dict) and s.get("name") == name:
            s["version"] = version
            s["active"] = bool(active)
            found = True
            break
    if not found:
        skills.append({"name": name, "version": version, "active": bool(active), "dev": bool(dev)})
    else:
        try:
            s["dev"] = bool(dev)  # type: ignore[name-defined]
        except Exception:
            pass
    _save_node_yaml(data, base_dir)
    _emit_capacity_changed(base_dir=base_dir)


def uninstall_skill_from_capacity(name: str, *, base_dir: Path | None = None) -> None:
    if _mutate_local_capacity(lambda payload: payload.__setitem__("skills", [s for s in list(payload.get("skills") or []) if not (isinstance(s, dict) and s.get("name") == name)]), base_dir=base_dir):
        _emit_capacity_changed(base_dir=base_dir)
        return
    data = _load_node_yaml(base_dir)
    cap = data.setdefault("capacity", {})
    skills: List[Dict[str, Any]] = cap.setdefault("skills", [])  # type: ignore[assignment]
    cap["skills"] = [s for s in skills if not (isinstance(s, dict) and s.get("name") == name)]
    _save_node_yaml(data, base_dir)
    _emit_capacity_changed(base_dir=base_dir)


def install_scenario_in_capacity(name: str, version: str, *, active: bool = True, dev: bool = False, base_dir: Path | None = None) -> None:
    if _mutate_local_capacity(
        lambda payload: _install_scenario_record(payload, name, version, active=active, dev=dev),
        base_dir=base_dir,
    ):
        _emit_capacity_changed(base_dir=base_dir)
        return
    data = _load_node_yaml(base_dir)
    cap = data.setdefault("capacity", {})
    scenarios: List[Dict[str, Any]] = cap.setdefault("scenarios", [])  # type: ignore[assignment]
    found = False
    for s in scenarios:
        if isinstance(s, dict) and s.get("name") == name:
            s["version"] = version
            s["active"] = bool(active)
            s["dev"] = bool(dev)
            found = True
            break
    if not found:
        scenarios.append({"name": name, "version": version, "active": bool(active), "dev": bool(dev)})
    _save_node_yaml(data, base_dir)
    _emit_capacity_changed(base_dir=base_dir)


def uninstall_scenario_from_capacity(name: str, *, base_dir: Path | None = None) -> None:
    if _mutate_local_capacity(lambda payload: payload.__setitem__("scenarios", [s for s in list(payload.get("scenarios") or []) if not (isinstance(s, dict) and s.get("name") == name)]), base_dir=base_dir):
        _emit_capacity_changed(base_dir=base_dir)
        return
    data = _load_node_yaml(base_dir)
    cap = data.setdefault("capacity", {})
    scenarios: List[Dict[str, Any]] = cap.setdefault("scenarios", [])  # type: ignore[assignment]
    cap["scenarios"] = [s for s in scenarios if not (isinstance(s, dict) and s.get("name") == name)]
    _save_node_yaml(data, base_dir)
    _emit_capacity_changed(base_dir=base_dir)


def install_io_in_capacity(io_type: str, capabilities: List[str] | None = None, *, priority: int = 50, id_hint: str | None = None, base_dir: Path | None = None) -> None:
    if _mutate_local_capacity(
        lambda payload: _install_io_record(payload, io_type, capabilities or [], priority=priority, id_hint=id_hint),
        base_dir=base_dir,
    ):
        _emit_capacity_changed(base_dir=base_dir)
        return
    data = _load_node_yaml(base_dir)
    cap = data.setdefault("capacity", {})
    io: List[Dict[str, Any]] = cap.setdefault("io", [])  # type: ignore[assignment]
    caps = list(capabilities or [])
    done = False
    for item in io:
        if isinstance(item, dict) and item.get("io_type") == io_type:
            item["capabilities"] = caps
            item["priority"] = int(priority)
            if id_hint is not None:
                item["id_hint"] = id_hint
            done = True
            break
    if not done:
        rec = {"io_type": io_type, "capabilities": caps, "priority": int(priority)}
        if id_hint is not None:
            rec["id_hint"] = id_hint
        io.append(rec)
    _save_node_yaml(data, base_dir)
    _emit_capacity_changed(base_dir=base_dir)


def uninstall_io_from_capacity(io_type: str, *, base_dir: Path | None = None) -> None:
    if _mutate_local_capacity(lambda payload: payload.__setitem__("io", [it for it in list(payload.get("io") or []) if not (isinstance(it, dict) and it.get("io_type") == io_type)]), base_dir=base_dir):
        _emit_capacity_changed(base_dir=base_dir)
        return
    data = _load_node_yaml(base_dir)
    cap = data.setdefault("capacity", {})
    io: List[Dict[str, Any]] = cap.setdefault("io", [])  # type: ignore[assignment]
    cap["io"] = [it for it in io if not (isinstance(it, dict) and it.get("io_type") == io_type)]
    _save_node_yaml(data, base_dir)
    _emit_capacity_changed(base_dir=base_dir)


def _install_skill_record(payload: Dict[str, Any], name: str, version: str, *, active: bool, dev: bool) -> None:
    skills: List[Dict[str, Any]] = payload.setdefault("skills", [])  # type: ignore[assignment]
    found = False
    for item in skills:
        if isinstance(item, dict) and item.get("name") == name:
            item["version"] = version
            item["active"] = bool(active)
            item["dev"] = bool(dev)
            found = True
            break
    if not found:
        skills.append({"name": name, "version": version, "active": bool(active), "dev": bool(dev)})


def _install_scenario_record(payload: Dict[str, Any], name: str, version: str, *, active: bool, dev: bool) -> None:
    scenarios: List[Dict[str, Any]] = payload.setdefault("scenarios", [])  # type: ignore[assignment]
    found = False
    for item in scenarios:
        if isinstance(item, dict) and item.get("name") == name:
            item["version"] = version
            item["active"] = bool(active)
            item["dev"] = bool(dev)
            found = True
            break
    if not found:
        scenarios.append({"name": name, "version": version, "active": bool(active), "dev": bool(dev)})


def _install_io_record(payload: Dict[str, Any], io_type: str, capabilities: List[str], *, priority: int, id_hint: str | None) -> None:
    io_items: List[Dict[str, Any]] = payload.setdefault("io", [])  # type: ignore[assignment]
    caps = list(capabilities or [])
    for item in io_items:
        if isinstance(item, dict) and item.get("io_type") == io_type:
            item["capabilities"] = caps
            item["priority"] = int(priority)
            if id_hint is not None:
                item["id_hint"] = id_hint
            return
    record = {"io_type": io_type, "capabilities": caps, "priority": int(priority)}
    if id_hint is not None:
        record["id_hint"] = id_hint
    io_items.append(record)


def _short_reason(value: str, *, limit: int = 140) -> str:
    s = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 3].rstrip() + "..."


def _set_io_state(
    io_type: str,
    *,
    available: bool,
    base_capabilities: List[str] | None = None,
    reason: str | None = None,
    mode: str | None = None,
    priority: int = 50,
    base_dir: Path | None = None,
) -> None:
    """
    Persist IO availability in the local capacity projection.

    This is intentionally encoded into `capabilities` to propagate via hub registry
    (which currently persists `capabilities_json` only).
    """
    caps = list(base_capabilities or [])
    caps.append("state:available" if available else "state:unavailable")
    if mode:
        caps.append(f"mode:{str(mode).strip().lower()}")
    if not available and reason:
        caps.append(f"reason:{_short_reason(reason)}")
    install_io_in_capacity(io_type, caps, priority=priority, base_dir=base_dir)


def set_io_state(
    io_type: str,
    *,
    available: bool,
    base_capabilities: List[str] | None = None,
    reason: str | None = None,
    mode: str | None = None,
    priority: int = 50,
    base_dir: Path | None = None,
) -> None:
    """
    Public wrapper around `_set_io_state` to persist IO availability into the local capacity projection.

    Note: this is encoded into the `capabilities` list for now (backward-compatible).
    """
    _set_io_state(
        io_type,
        available=available,
        base_capabilities=base_capabilities,
        reason=reason,
        mode=mode,
        priority=priority,
        base_dir=base_dir,
    )


def get_io_capacity_entry(io_type: str, *, base_dir: Path | None = None) -> dict[str, Any] | None:
    cap = get_local_capacity()
    io_list = cap.get("io") or []
    for it in io_list:
        if isinstance(it, dict) and str(it.get("io_type") or "") == io_type:
            return it
    return None


def io_capacity_capabilities(entry: dict[str, Any] | None) -> list[str]:
    if not isinstance(entry, dict):
        return []
    out: list[str] = []
    for item in list(entry.get("capabilities") or []):
        token = str(item or "").strip()
        if token:
            out.append(token)
    return out


def io_capacity_token_values(entry: dict[str, Any] | None, prefix: str) -> list[str]:
    prefix_norm = f"{str(prefix or '').strip().lower()}:"
    if prefix_norm == ":":
        return []
    out: list[str] = []
    for item in io_capacity_capabilities(entry):
        if item.lower().startswith(prefix_norm):
            value = item.split(":", 1)[1].strip()
            if value:
                out.append(value)
    return out


def io_capacity_first_token_value(entry: dict[str, Any] | None, prefix: str) -> str | None:
    values = io_capacity_token_values(entry, prefix)
    return values[0] if values else None


def is_io_capacity_entry_available(entry: dict[str, Any] | None, *, default: bool = True) -> bool:
    caps = io_capacity_capabilities(entry)
    if any(c.startswith("state:unavailable") for c in caps):
        return False
    if any(c.startswith("mode:disabled") for c in caps):
        return False
    if any(c.startswith("state:available") for c in caps):
        return True
    return default


def is_io_available(io_type: str, *, base_dir: Path | None = None, default: bool = True) -> bool:
    it = get_io_capacity_entry(io_type, base_dir=base_dir)
    return is_io_capacity_entry_available(it, default=default)


def refresh_native_io_capacity(*, base_dir: Path | None = None) -> Dict[str, Any]:
    """
    Detect optional native IO dependencies and reflect them in the local capacity projection.

    - `say` reflects local TTS availability (pyttsx3).
    - `voice` reflects hub STT availability (vosk native library).
      Note: local microphone capture (sounddevice/PortAudio) is a separate concern.
    """
    out: Dict[str, Any] = {"updated": [], "warnings": []}

    # TTS: pyttsx3
    try:
        from adaos.adapters.audio.tts import native_tts as _native_tts

        available = bool(getattr(_native_tts, "pyttsx3", None) is not None)
        reason = None if available else str(getattr(_native_tts, "_import_error", "") or "pyttsx3 import failed")
        _set_io_state(
            "say",
            available=available,
            base_capabilities=["text", "lang:ru", "lang:en", "tts:native"],
            reason=reason,
            priority=40,
            base_dir=base_dir,
        )
        out["updated"].append({"io_type": "say", "available": available})
    except Exception as exc:
        out["warnings"].append(f"say detect failed: {exc}")

    # STT: Vosk (hub STT API)
    try:
        available = True
        reason = None
        try:
            import vosk  # type: ignore  # noqa: F401
        except Exception as exc:
            available = False
            reason = str(exc)
        _set_io_state(
            "voice",
            available=available,
            base_capabilities=["audio", "stt:vosk", "api:/api/stt/transcribe"],
            reason=reason,
            priority=30,
            base_dir=base_dir,
        )
        out["updated"].append({"io_type": "voice", "available": available})
    except Exception as exc:
        out["warnings"].append(f"voice detect failed: {exc}")

    # WebRTC AV runtime: aiortc
    try:
        from adaos.services.agent_context import get_ctx
        from adaos.services.media_capability import local_webrtc_media_capabilities

        available = True
        reason = None
        try:
            import aiortc  # type: ignore  # noqa: F401
        except Exception as exc:
            available = False
            reason = str(exc)
        role = str(getattr(get_ctx().config, "role", "") or "")
        _set_io_state(
            "webrtc_media",
            available=available,
            base_capabilities=local_webrtc_media_capabilities(role=role),
            reason=reason,
            mode="webrtc",
            priority=35,
            base_dir=base_dir,
        )
        out["updated"].append({"io_type": "webrtc_media", "available": available})
    except Exception as exc:
        out["warnings"].append(f"webrtc_media detect failed: {exc}")

    return out
