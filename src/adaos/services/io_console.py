"""Minimal console output adapter used by the sandbox scenario runner."""

from __future__ import annotations

import os
import sys


def print(text: str | None) -> dict[str, bool]:
    """Write ``text`` to stdout and flush immediately."""

    if text is None:
        return {"ok": True}
    if os.getenv("ADAOS_TESTING"):
        return {"ok": True}
    sys.stdout.write(f"{text.rstrip()}\n")
    sys.stdout.flush()
    return {"ok": True}


def print_text(text: str, node_id: str | None = None, origin: dict | None = None) -> None:
    prefix = f"[IO/console@{node_id}]" if node_id else "[IO/console]"
    sys.stdout.write(f"{prefix} {text.rstrip()}\n")
    sys.stdout.flush()


__all__ = ["print", "print_text"]
