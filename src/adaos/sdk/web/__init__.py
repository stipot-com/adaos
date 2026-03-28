from __future__ import annotations

"""
High-level helpers for interacting with the web desktop and web IO.

These functions provide a thin, LLM-friendly facade over internal
services such as :class:`adaos.services.io_web.WebDesktopService`.
Skills should use this layer instead of touching YDoc or Yjs APIs
directly.
"""

from .desktop import (
    desktop_get_installed,
    desktop_get_pinned_widgets,
    desktop_set_installed,
    desktop_set_pinned_widgets,
    desktop_toggle_install,
    desktop_toggle_app,
    desktop_toggle_widget,
)
from .webspace import (
    webspace_list,
    webspace_describe,
    webspace_create,
    webspace_rename,
    webspace_delete,
    webspace_refresh,
    webspace_set_home,
    webspace_go_home,
    webspace_ensure_dev,
)

__all__ = [
    "desktop_toggle_install",
    "desktop_toggle_app",
    "desktop_toggle_widget",
    "desktop_get_installed",
    "desktop_set_installed",
    "desktop_get_pinned_widgets",
    "desktop_set_pinned_widgets",
    "webspace_list",
    "webspace_describe",
    "webspace_create",
    "webspace_rename",
    "webspace_delete",
    "webspace_refresh",
    "webspace_set_home",
    "webspace_go_home",
    "webspace_ensure_dev",
]
