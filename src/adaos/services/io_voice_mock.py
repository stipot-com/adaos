"""Mock voice IO adapters (TTS/STT) for local development."""

from __future__ import annotations

import sys

from .io_console import print as console_print
from adaos.sdk.data.env import get_tts_backend
from adaos.adapters.audio.tts.native_tts import NativeTTS
from adaos.integrations.rhasspy.tts import RhasspyTTSAdapter


def tts_speak(text: str | None) -> dict[str, bool]:
    """Speak text via the configured TTS backend, fallback to console."""

    if not text:
        return {"ok": True}

    try:
        mode = get_tts_backend()
    except Exception:
        mode = "native"

    try:
        adapter = RhasspyTTSAdapter() if mode == "rhasspy" else NativeTTS()
        adapter.say(text)
        return {"ok": True}
    except Exception:
        console_print(f"[TTS] {text}")
        return {"ok": True}


def stt_listen(timeout: str = "20s") -> dict[str, str]:
    """Read a line from stdin to emulate a STT response."""

    console_print("[STT] эмуляция ввода имени: введите имя и нажмите Enter")
    try:
        line = sys.stdin.readline()
    except Exception:
        return {}
    text = line.strip()
    return {"text": text} if text else {}


__all__ = ["tts_speak", "stt_listen"]
