"""A small in-process sliding-window rate limiter.

The app has no authentication and runs behind a single Gunicorn worker, so a
handful of expensive requests from one client can occupy every worker thread.
This keeps per-client request rates bounded without adding a dependency or an
external store; it is per-process state, which is exact for the current
single-worker deployment and still a useful bound if more workers are added.

Memory is capped: timestamps outside the window are dropped on access, and the
whole table is swept once it exceeds ``max_tracked`` keys.
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
            if len(self._hits) > self._max_tracked:
                self._sweep_locked(now, window)

            timestamps = [t for t in self._hits.get(key, ()) if t > cutoff]

            if len(timestamps) >= limit:
                self._hits[key] = timestamps
                retry_after = max(1, int(timestamps[0] + window - now) + 1)
                return False, retry_after

            timestamps.append(now)
            self._hits[key] = timestamps
            return True, 0

    def _sweep_locked(self, now: float, window: float) -> None:
        cutoff = now - window
        for key in list(self._hits):
            remaining = [t for t in self._hits[key] if t > cutoff]
            if remaining:
                self._hits[key] = remaining
            else:
                del self._hits[key]
        # Still oversized (many distinct clients inside one window): drop the
        # coldest keys rather than growing without bound.
        if len(self._hits) > self._max_tracked:
            for key in sorted(self._hits, key=lambda k: self._hits[k][-1])[
                : len(self._hits) - self._max_tracked
            ]:
                del self._hits[key]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
