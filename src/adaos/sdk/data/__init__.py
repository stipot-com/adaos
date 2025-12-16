"""Data-plane helpers exposed by the AdaOS SDK.

This module is intentionally import-light: it avoids eager imports that pull in
runtime services (scenario/yjs/etc.) so that service-layer modules can safely
depend on small SDK utilities without creating circular imports.
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "BusNotAvailable",
    "emit",
    "on",
    "get_meta",
    "clear_current_skill",
    "set_current_skill",
    "get_current_skill",
    "publish",
    "tmp_path",
    "save_bytes",
    "open",
    "get",
    "put",
    "delete",
    "list",
    "read",
    "write",
    "profile_get_settings",
    "profile_update_settings",
    "ctx_subnet",
    "ctx_current_user",
    "ctx_selected_user",
    "I18n",
    "_",
    "skill_memory_get",
    "skill_memory_set",
    "get_tts_backend",
    "get_stt_backend",
    "get_audio_out_backend",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "BusNotAvailable": ("adaos.sdk.data.bus", "BusNotAvailable"),
    "emit": ("adaos.sdk.data.bus", "emit"),
    "on": ("adaos.sdk.data.bus", "on"),
    "get_meta": ("adaos.sdk.data.bus", "get_meta"),
    "clear_current_skill": ("adaos.sdk.data.context", "clear_current_skill"),
    "set_current_skill": ("adaos.sdk.data.context", "set_current_skill"),
    "get_current_skill": ("adaos.sdk.data.context", "get_current_skill"),
    "get_audio_out_backend": ("adaos.sdk.data.env", "get_audio_out_backend"),
    "get_stt_backend": ("adaos.sdk.data.env", "get_stt_backend"),
    "get_tts_backend": ("adaos.sdk.data.env", "get_tts_backend"),
    "publish": ("adaos.sdk.data.events", "publish"),
    "open": ("adaos.sdk.data.fs", "open"),
    "save_bytes": ("adaos.sdk.data.fs", "save_bytes"),
    "tmp_path": ("adaos.sdk.data.fs", "tmp_path"),
    "I18n": ("adaos.sdk.data.i18n", "I18n"),
    "_": ("adaos.sdk.data.i18n", "_"),
    "delete": ("adaos.sdk.data.memory", "delete"),
    "get": ("adaos.sdk.data.memory", "get"),
    "list": ("adaos.sdk.data.memory", "list"),
    "put": ("adaos.sdk.data.memory", "put"),
    "profile_get_settings": ("adaos.sdk.data.profile", "get_settings"),
    "profile_update_settings": ("adaos.sdk.data.profile", "update_settings"),
    "ctx_subnet": ("adaos.sdk.data.ctx", "subnet"),
    "ctx_current_user": ("adaos.sdk.data.ctx", "current_user"),
    "ctx_selected_user": ("adaos.sdk.data.ctx", "selected_user"),
    "read": ("adaos.sdk.data.secrets", "read"),
    "write": ("adaos.sdk.data.secrets", "write"),
    "skill_memory_get": ("adaos.sdk.data.skill_memory", "get"),
    "skill_memory_set": ("adaos.sdk.data.skill_memory", "set"),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    mod, attr = target
    return getattr(import_module(mod), attr)

