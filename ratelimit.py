import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    # Sliding window: at most max_calls per user within per_seconds. In memory, single process (C-2).
    def __init__(self, max_calls, per_seconds):
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self.calls = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            window = self.calls[key]
            while window and now - window[0] > self.per_seconds:
                window.popleft()
            if len(window) >= self.max_calls:
                return False
            window.append(now)
            return True
