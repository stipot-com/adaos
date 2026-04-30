from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from typing import Any


def coerce_webspace_id(value: Any, *, fallback: str = "default") -> str:
    """Normalize webspace tokens passed through event metadata.

    Skills sometimes receive subscription payloads shaped as
    ``{"webspace_id": "default"}`` and accidentally pass that object back as
    a webspace id. This helper is deliberately outside the Yjs package so SDK
    output helpers can use it without triggering Yjs subscription registration.
    """

    default_token = str(fallback or "default").strip() or "default"

    def _coerce(node: Any) -> str:
        if isinstance(node, Mapping):
            for key in ("webspace_id", "workspace_id", "id"):
                nested = node.get(key)
                if nested is not None and nested is not node:
                    token = _coerce(nested)
                    if token:
                        return token
            return default_token
        if isinstance(node, Sequence) and not isinstance(node, (bytes, bytearray, str)):
            for item in node:
                token = _coerce(item)
                if token:
                    return token
            return default_token

        token = str(node or "").strip()
        if token.startswith(("{", "[")) and ("webspace_id" in token or "workspace_id" in token):
            try:
                parsed = ast.literal_eval(token)
            except (SyntaxError, ValueError):
                parsed = None
            if parsed is not None and parsed is not node:
                parsed_token = _coerce(parsed)
                if parsed_token:
                    return parsed_token
        return token or default_token

    return _coerce(value)
