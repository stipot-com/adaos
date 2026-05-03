from __future__ import annotations


_RESERVED_DATA_ROOTS = {
    "adaos_connect",
    "catalog",
    "desktop",
    "installed",
    "media",
    "nlu",
    "nlu_teacher",
    "routing",
    "scenarios",
    "tts",
    "voice_chat",
    "webio",
    "webspaces",
}


def node_scope_data_path(path: str | None, node_id: str | None) -> str:
    """
    Return a browser/Yjs data path isolated under ``data/nodes/<node_id>``.

    The desktop keeps runtime-owned branches such as ``data.catalog`` and
    ``data.desktop`` shared. Skill-owned state can use the same local path on
    each node while the shared webspace sees a node-scoped envelope.
    """

    raw = str(path or "").strip()
    node = str(node_id or "").strip()
    if not raw or not node:
        return raw

    prefix = ""
    body = raw
    if body.startswith("y:"):
        prefix = "y:"
        body = body[2:]

    parts = [part for part in body.split("/") if part]
    if len(parts) < 2 or parts[0] != "data":
        return raw
    if parts[1] == "nodes" or parts[1] in _RESERVED_DATA_ROOTS:
        return raw

    return f"{prefix}data/nodes/{node}/{'/'.join(parts[1:])}"


def is_node_scoped_data_path(path: str | None) -> bool:
    raw = str(path or "").strip()
    if raw.startswith("y:"):
        raw = raw[2:]
    parts = [part for part in raw.split("/") if part]
    return len(parts) >= 3 and parts[0] == "data" and parts[1] == "nodes"


__all__ = ["is_node_scoped_data_path", "node_scope_data_path"]
