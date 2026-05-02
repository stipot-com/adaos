from __future__ import annotations

import uuid
from typing import Any, Mapping

from adaos.services.node_runtime_state import load_node_display_runtime_state

_NODE_COLOR_PALETTE: tuple[str, ...] = (
    "#4E79A7",
    "#F28E2B",
    "#59A14F",
    "#E15759",
    "#76B7B2",
    "#EDC948",
    "#B07AA1",
    "#FF9DA7",
    "#9C755F",
    "#BAB0AC",
    "#5B8FF9",
    "#61DDAA",
    "#65789B",
    "#F6BD16",
    "#7262FD",
    "#78D3F8",
)

_GENERIC_NODE_NAMES = {
    "hub",
    "member",
    "node",
    "default",
    "desktop",
    "webspace",
    "workspace",
    "local",
}


def node_color_palette() -> tuple[str, ...]:
    return _NODE_COLOR_PALETTE


def _normalize_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except Exception:
        return None
    return normalized if normalized >= 0 else None


def _looks_like_uuid(value: str | None) -> bool:
    token = str(value or "").strip()
    if not token:
        return False
    try:
        parsed = uuid.UUID(token)
    except Exception:
        return False
    return str(parsed) == token.lower()


def _noise_node_name(value: Any, *, role: str | None = None) -> bool:
    token = str(value or "").strip()
    if not token:
        return True
    lowered = token.casefold()
    if lowered in _GENERIC_NODE_NAMES:
        return True
    if role and lowered == str(role or "").strip().casefold():
        return True
    if token.startswith("{") and token.endswith("}"):
        return True
    if _looks_like_uuid(token):
        return True
    return False


def normalize_node_names(value: Any, *, role: str | None = None) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            continue
        token = str(item or "").strip()
        if _noise_node_name(token, role=role):
            continue
        folded = token.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        out.append(token)
    return out


def fallback_node_label(index: int | None) -> str:
    normalized = _normalize_int(index)
    if normalized is None:
        return "Node"
    return f"Node {normalized}"


def compact_node_label(index: int | None) -> str:
    normalized = _normalize_int(index)
    if normalized is None:
        return "N?"
    return f"N{normalized}"


def resolve_node_color(index: Any) -> tuple[int, str]:
    palette = node_color_palette()
    normalized = _normalize_int(index) or 0
    accent_index = normalized % len(palette)
    return accent_index, palette[accent_index]


def node_display_payload(
    *,
    node_id: str,
    role: str,
    node_names: Any = None,
    primary_node_name: Any = None,
    display_index: Any = None,
    accent_index: Any = None,
) -> dict[str, Any]:
    role_norm = str(role or "").strip().lower()
    resolved_index = _normalize_int(display_index)
    if resolved_index is None and role_norm == "hub":
        resolved_index = 0
    if resolved_index is None and role_norm == "member":
        resolved_index = 1
    meaningful_names = normalize_node_names(node_names, role=role_norm)
    primary = str(primary_node_name or "").strip()
    explicit_name = meaningful_names[0] if meaningful_names else (
        None if _noise_node_name(primary, role=role_norm) else primary
    )
    node_label = explicit_name or fallback_node_label(resolved_index)
    compact_label = compact_node_label(resolved_index)
    resolved_accent_index = _normalize_int(accent_index)
    if resolved_accent_index is None:
        resolved_accent_index, node_color = resolve_node_color(resolved_index)
    else:
        palette = node_color_palette()
        node_color = palette[resolved_accent_index % len(palette)]
    return {
        "node_id": str(node_id or "").strip(),
        "node_label": node_label,
        "node_compact_label": compact_label,
        "node_index": resolved_index,
        "node_color": node_color,
        "node_color_index": resolved_accent_index,
        "node_has_explicit_name": bool(explicit_name),
    }


def node_display_from_config(conf: Any) -> dict[str, Any]:
    role = str(getattr(conf, "role", "") or "").strip().lower()
    node_id = str(getattr(conf, "node_id", "") or "").strip()
    node_names = list(getattr(conf, "node_names", []) or [])
    assignment = load_node_display_runtime_state() if role == "member" else {}
    return node_display_payload(
        node_id=node_id,
        role=role,
        node_names=node_names,
        primary_node_name=str(getattr(conf, "primary_node_name", "") or ""),
        display_index=assignment.get("display_index"),
        accent_index=assignment.get("accent_index"),
    )


def node_display_from_directory_node(node: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = node if isinstance(node, Mapping) else {}
    runtime_projection = (
        payload.get("runtime_projection")
        if isinstance(payload.get("runtime_projection"), Mapping)
        else {}
    )
    snapshot = (
        runtime_projection.get("snapshot")
        if isinstance(runtime_projection.get("snapshot"), Mapping)
        else {}
    )
    roles = [
        str(item or "").strip().lower()
        for item in list(payload.get("roles") or [])
        if str(item or "").strip()
    ]
    role = "member"
    if "hub" in roles and "member" not in roles:
        role = "hub"
    node_names = (
        runtime_projection.get("node_names")
        if isinstance(runtime_projection.get("node_names"), list)
        else snapshot.get("node_names")
    )
    return node_display_payload(
        node_id=str(payload.get("node_id") or "").strip(),
        role=role,
        node_names=node_names,
        primary_node_name=(
            runtime_projection.get("primary_node_name")
            or snapshot.get("primary_node_name")
            or ""
        ),
        display_index=payload.get("display_index"),
        accent_index=payload.get("accent_index"),
    )
