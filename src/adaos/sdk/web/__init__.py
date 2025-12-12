from __future__ import annotations

"""
High-level helpers for interacting with the web desktop and web IO.

These functions provide a thin, LLM-friendly facade over internal
services such as :class:`adaos.services.io_web.WebDesktopService`.
Skills should use this layer instead of touching YDoc or Yjs APIs
directly.
"""

from .desktop import desktop_toggle_install, desktop_toggle_app, desktop_toggle_widget
from .webspace import (
    webspace_list,
    webspace_create,
    webspace_rename,
    webspace_delete,
    webspace_refresh,
)

__all__ = [
    "desktop_toggle_install",
    "desktop_toggle_app",
    "desktop_toggle_widget",
    "webspace_list",
    "webspace_create",
    "webspace_rename",
    "webspace_delete",
    "webspace_refresh",
]
