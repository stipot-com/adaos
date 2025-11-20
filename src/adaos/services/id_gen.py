from __future__ import annotations

import os
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_BITS = 80
_TIME_BITS = 48
_MAX_RANDOM = (1 << _RANDOM_BITS) - 1
_LOCK = threading.Lock()
_last_timestamp = 0
_last_random = 0


def _encode_ulid(value: int) -> str:
    chars: list[str] = []
    for _ in range(26):
        value, idx = divmod(value, 32)
        chars.append(_ALPHABET[idx])
    return "".join(reversed(chars))


def new_id() -> str:
    global _last_timestamp, _last_random
    millis = int(time.time() * 1000)
    with _LOCK:
        if millis > _last_timestamp:
            _last_timestamp = millis
            _last_random = int.from_bytes(os.urandom(10), "big")
        else:
            millis = _last_timestamp
            _last_random = (_last_random + 1) & _MAX_RANDOM
            if _last_random == 0:
                while millis <= _last_timestamp:
                    time.sleep(0.001)
                    millis = int(time.time() * 1000)
                _last_timestamp = millis
                _last_random = int.from_bytes(os.urandom(10), "big")
        value = (_last_timestamp << _RANDOM_BITS) | _last_random
    return _encode_ulid(value)


__all__ = ["new_id"]
