"""Concurrency + rate limiting for the local proxy.

Immersive Translate fires a whole page's worth of paragraphs at once, which is
exactly the traffic pattern that trips the upstream free service's limiter. The
gate keeps at most N requests in flight and smooths the rest through a token
bucket, so a 60-paragraph article becomes a gentle drip instead of a burst.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator


class TokenBucket:
    """Classic token bucket: ``rate`` tokens refilled per second, capped at ``burst``."""

    def __init__(self, rate: float, burst: int | float) -> None:
        self._rate = max(0.0, float(rate))
        self._burst = max(1.0, float(burst))
        self._tokens = self._burst
        self._stamp = time.monotonic()
        self._lock = threading.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._stamp
        if elapsed <= 0:
            return
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._stamp = now

    def acquire(self, timeout: float) -> bool:
        """Take one token, waiting at most ``timeout`` seconds."""
        if self._rate <= 0:  # unlimited
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                wait = (1.0 - self._tokens) / self._rate
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # Sleep only as long as still needed, and re-check so a policy
            # change (or a burst of releases) is picked up quickly.
            time.sleep(min(wait, remaining, 0.2))


class RequestGate:
    """Bounded concurrency plus a token bucket, with queueing and counters."""

    def __init__(
        self,
        *,
        max_concurrency: int = 4,
        qps: float = 3.0,
        burst: int = 6,
        queue_timeout: float = 90.0,
    ) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self.qps = float(qps)
        self.burst = max(1, int(burst))
        self.queue_timeout = float(queue_timeout)
        self._sem = threading.BoundedSemaphore(self.max_concurrency)
        self._bucket = TokenBucket(self.qps, self.burst)
        self._lock = threading.Lock()
        self._inflight = 0
        self._served = 0
        self._rejected = 0

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, float]:
        with self._lock:
            return {
                "inflight": self._inflight,
                "served": self._served,
                "rejected": self._rejected,
                "max_concurrency": self.max_concurrency,
                "qps": self.qps,
            }

    def note_served(self) -> None:
        with self._lock:
            self._served += 1

    # -------------------------------------------------------------------- gate
    @contextmanager
    def slot(self, timeout: float | None = None) -> Iterator[bool]:
        """Yield True when the caller may proceed, False when it was shed."""
        budget = self.queue_timeout if timeout is None else float(timeout)
        started = time.monotonic()
        if not self._sem.acquire(timeout=max(0.0, budget)):
            with self._lock:
                self._rejected += 1
            yield False
            return
        try:
            remaining = max(0.0, budget - (time.monotonic() - started))
            if not self._bucket.acquire(remaining):
                with self._lock:
                    self._rejected += 1
                yield False
                return
            with self._lock:
                self._inflight += 1
                self._served += 1
            try:
                yield True
            finally:
                with self._lock:
                    self._inflight -= 1
        finally:
            self._sem.release()
