from __future__ import annotations

from dataclasses import dataclass

SERVICE_NAME = "adaos-root/api.inimatic.com"


class KeyringUnavailableError(RuntimeError):
    """Raised when the system keyring backend is not available."""


def _username(owner_id: str) -> str:
    return f"owner:{owner_id}"


def _require_keyring():
    try:
        import keyring  # type: ignore
    except Exception as exc:  # pragma: no cover - import failure path
        raise KeyringUnavailableError("system keyring is unavailable") from exc
    return keyring


def save_refresh(owner_id: str, refresh_token: str) -> None:
    keyring = _require_keyring()
    try:
        keyring.set_password(SERVICE_NAME, _username(owner_id), refresh_token)
    except Exception as exc:  # pragma: no cover - backend specific errors
        raise KeyringUnavailableError("failed to write refresh token to keyring") from exc


def load_refresh(owner_id: str) -> str | None:
    keyring = _require_keyring()
    try:
        token = keyring.get_password(SERVICE_NAME, _username(owner_id))
    except Exception as exc:  # pragma: no cover
        raise KeyringUnavailableError("failed to load refresh token from keyring") from exc
    return token or None


def delete_refresh(owner_id: str) -> None:
    keyring = _require_keyring()
    try:
        keyring.delete_password(SERVICE_NAME, _username(owner_id))
    except keyring.errors.PasswordDeleteError:  # type: ignore[attr-defined]
        return
    except Exception as exc:  # pragma: no cover
        raise KeyringUnavailableError("failed to delete refresh token from keyring") from exc


__all__ = [
    "KeyringUnavailableError",
    "save_refresh",
    "load_refresh",
    "delete_refresh",
]
