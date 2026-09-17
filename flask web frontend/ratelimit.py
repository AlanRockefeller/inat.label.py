"""A small in-process sliding-window rate limiter.

The app has no authentication and runs behind a single Gunicorn worker, so a
handful of expensive requests from one client can occupy every worker thread.
This keeps per-client request rates bounded without adding a dependency or an
external store; it is per-process state, which is exact for the current
single-worker deployment and still a useful bound if more workers are added.

Memory is capped: timestamps outside each key's own window are dropped on
access.  Once ``max_tracked`` active keys exist, new keys fail closed until a
slot expires; active quota state is never evicted to make room.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, max_tracked: int = 20000, time_source=time.monotonic):
        self._hits: dict = {}
        self._lock = threading.Lock()
        self._max_tracked = max_tracked
        self._time = time_source

    def check(self, key, limit: int, window: float) -> tuple:
        """Record a hit for ``key``.

        Returns ``(allowed, retry_after_seconds)``.  A rejected request is not
        counted, so a client that keeps hammering a closed window does not push
        its own reset further out.
        """
        if limit <= 0:
            return False, window

        now = self._time()
        cutoff = now - window

        with self._lock:
            if key not in self._hits and len(self._hits) >= self._max_tracked:
                self._sweep_locked(now)
                if len(self._hits) >= self._max_tracked:
                    if not self._hits:
                        return False, max(1, int(window))
                    retry_after = min(
                        max(1, int(state["timestamps"][0] + state["window"] - now) + 1)
                        for state in self._hits.values()
                    )
                    return False, retry_after

            state = self._hits.get(key)
            timestamps = [
                timestamp
                for timestamp in (state or {}).get("timestamps", ())
                if timestamp > cutoff
            ]

            if len(timestamps) >= limit:
                self._hits[key] = {"timestamps": timestamps, "window": window}
                retry_after = max(1, int(timestamps[0] + window - now) + 1)
                return False, retry_after

            timestamps.append(now)
            self._hits[key] = {"timestamps": timestamps, "window": window}
            return True, 0

    def _sweep_locked(self, now: float) -> None:
        for key in list(self._hits):
            state = self._hits[key]
            cutoff = now - state["window"]
            remaining = [t for t in state["timestamps"] if t > cutoff]
            if remaining:
                state["timestamps"] = remaining
            else:
                del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
