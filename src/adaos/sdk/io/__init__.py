"""SDK facades for IO helpers (voice/console/etc).

This module intentionally avoids importing submodules eagerly to prevent
import cycles (e.g. decorators -> io.context -> io package).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["chat_append", "say", "media_route", "stream_publish", "stt_listen", "tts_speak"]

if TYPE_CHECKING:
    from .out import chat_append, say, media_route, stream_publish
    from .voice import stt_listen, tts_speak


def __getattr__(name: str) -> Any:  # pragma: no cover
    if name in ("chat_append", "say", "media_route", "stream_publish"):
        from .out import chat_append, say, media_route, stream_publish

        return {
            "chat_append": chat_append,
            "say": say,
            "media_route": media_route,
            "stream_publish": stream_publish,
        }[name]
    if name in ("stt_listen", "tts_speak"):
        from .voice import stt_listen, tts_speak

        return {"stt_listen": stt_listen, "tts_speak": tts_speak}[name]
    raise AttributeError(name)


def __dir__() -> list[str]:  # pragma: no cover
    return sorted(list(globals().keys()) + __all__)
