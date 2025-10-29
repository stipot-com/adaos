from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
import yaml


def load_capacity_from_node_yaml(base_dir: Path | None = None) -> Dict[str, Any]:
    """
    Reads node.yaml from the active base dir and returns a minimal capacity snapshot.
    Structure example:
    {
      "io": [
        {"io_type": "stdout", "capabilities": ["text", "lang:ru", "lang:en"], "priority": 50}
      ]
    }
    """
    # Resolve .adaos base dir lazily from env/context if not provided
    if base_dir is None:
        try:
            from adaos.services.node_config import node_base_dir

            base_dir = node_base_dir()
        except Exception:
            base_dir = Path.home() / ".adaos"

    node_path = Path(base_dir) / "node.yaml"
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

    return {"io": io_list, "skills": skills_list, "scenarios": scenarios_list}


def get_local_capacity() -> Dict[str, Any]:
    return load_capacity_from_node_yaml()


# ----- mutation helpers for node.yaml -----

def _resolve_base_dir(base_dir: Path | None = None) -> Path:
    if base_dir is not None:
        return base_dir
    try:
        from adaos.services.node_config import node_base_dir

        return node_base_dir()
    except Exception:
        return Path.home() / ".adaos"


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


def install_skill_in_capacity(name: str, version: str, *, active: bool = True, dev: bool = False, base_dir: Path | None = None) -> None:
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


def uninstall_skill_from_capacity(name: str, *, base_dir: Path | None = None) -> None:
    data = _load_node_yaml(base_dir)
    cap = data.setdefault("capacity", {})
    skills: List[Dict[str, Any]] = cap.setdefault("skills", [])  # type: ignore[assignment]
    cap["skills"] = [s for s in skills if not (isinstance(s, dict) and s.get("name") == name)]
    _save_node_yaml(data, base_dir)


def install_scenario_in_capacity(name: str, version: str, *, active: bool = True, dev: bool = False, base_dir: Path | None = None) -> None:
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


def uninstall_scenario_from_capacity(name: str, *, base_dir: Path | None = None) -> None:
    data = _load_node_yaml(base_dir)
    cap = data.setdefault("capacity", {})
    scenarios: List[Dict[str, Any]] = cap.setdefault("scenarios", [])  # type: ignore[assignment]
    cap["scenarios"] = [s for s in scenarios if not (isinstance(s, dict) and s.get("name") == name)]
    _save_node_yaml(data, base_dir)


def install_io_in_capacity(io_type: str, capabilities: List[str] | None = None, *, priority: int = 50, id_hint: str | None = None, base_dir: Path | None = None) -> None:
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


def uninstall_io_from_capacity(io_type: str, *, base_dir: Path | None = None) -> None:
    data = _load_node_yaml(base_dir)
    cap = data.setdefault("capacity", {})
    io: List[Dict[str, Any]] = cap.setdefault("io", [])  # type: ignore[assignment]
    cap["io"] = [it for it in io if not (isinstance(it, dict) and it.get("io_type") == io_type)]
    _save_node_yaml(data, base_dir)
