"""Rate limiting primitives."""

from __future__ import annotations

import time
from collections import defaultdict, deque


class TokenBucket:
    """Allows `rate` actions per second on average with bursts up to `burst`."""

    def __init__(self, rate: float, burst: int, clock=time.monotonic) -> None:
        self.rate = rate
        self.burst = burst
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock()

    def allow(self, cost: float = 1.0) -> bool:
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
        self._last = now
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False


class LoginLimiter:
    """Limits failed login attempts per key (IP).

    5 failures per minute -> blocked 5 minutes; 20 per hour -> blocked 1 hour.
    """

    RULES = ((60.0, 5, 300.0), (3600.0, 20, 3600.0))

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}

    def blocked_for(self, key: str) -> float:
        until = self._blocked_until.get(key)
        if until is None:
            return 0.0
        left = until - self._clock()
        if left <= 0:
            del self._blocked_until[key]
            return 0.0
        return left

    def failure(self, key: str) -> bool:
        """Record a failure. Returns True if the key just became blocked."""
        now = self._clock()
        q = self._fails[key]
        q.append(now)
        longest = max(w for w, _, _ in self.RULES)
        while q and now - q[0] > longest:
            q.popleft()
        for window, limit, block in self.RULES:
            if sum(1 for t in q if now - t <= window) >= limit:
                self._blocked_until[key] = max(self._blocked_until.get(key, 0), now + block)
                return True
        return False

    def success(self, key: str) -> None:
        self._fails.pop(key, None)
