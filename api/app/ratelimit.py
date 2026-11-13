# HeArt v1 API — invented by John Pierce Davis Jr., Inventor and CEO.
"""Per-key sliding-window rate limiting. Day-one guardrail (§2 of the spec).

v1 uses an in-memory window: correct for a single process. PRODUCTION NOTE:
run behind Redis (or equivalent) when the API scales past one worker.
"""

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            cutoff = now - self.window
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= self.limit:
                return False, (dq[0] + self.window) - now
            dq.append(now)
            return True, 0.0
