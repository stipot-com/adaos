from __future__ import annotations
import time
from typing import Dict


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: int) -> None:
        self.rate = rate_per_sec
        self.capacity = capacity
        self.tokens = float(capacity)
        self.updated = time.monotonic()

    def allow(self, cost: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self.updated
        self.updated = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


class PerChatLimiter:
    def __init__(self, rate_per_sec: float = 1.0, capacity: int = 30) -> None:
        self.rate = rate_per_sec
        self.capacity = capacity
        self._buckets: Dict[str, TokenBucket] = {}

    def allow(self, chat_id: str, cost: float = 1.0) -> bool:
        b = self._buckets.get(chat_id)
        if not b:
            b = self._buckets[chat_id] = TokenBucket(self.rate, self.capacity)
        return b.allow(cost)

