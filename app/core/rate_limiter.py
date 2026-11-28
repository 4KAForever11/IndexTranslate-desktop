"""Concurrency + pacing gate shared by every translation entry point.

The free upstream service dislikes bursts: a batch of 20 images or a page of 60
paragraphs, fired at once, comes back as 429s. Two independent limits fix that:

* a bounded semaphore, so at most *N* requests are in flight;
* a minimum interval between starts, so even a single-at-a-time caller does not
  machine-gun the endpoint.

Waiting is polling rather than a blocking acquire, because a queued request must
be cancellable: the user presses 停止 and the worker thread has to leave the
queue within ~0.1 s instead of sitting there until a slot frees.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator

POLL = 0.1


class RateLimiter:
    def __init__(self, max_concurrency: int = 3, min_interval_sec: float = 0.25) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self.min_interval_sec = max(0.0, float(min_interval_sec))
        self._sem = threading.BoundedSemaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._next_start = 0.0
        self._inflight = 0
        self._served = 0
        self._rejected = 0

    # ------------------------------------------------------------------ public
    @contextmanager
    def acquire(self, cancel_event: threading.Event | None = None) -> Iterator[bool]:
        """Hold a slot for the duration of the block.

        Yields ``False`` when the caller was cancelled while queued; the caller
        must then return without doing any work.
        """
        if not self._acquire_slot(cancel_event):
            with self._lock:
                self._rejected += 1
            yield False
            return
        with self._lock:
            self._inflight += 1
        try:
            yield True
        finally:
            with self._lock:
                self._inflight -= 1
                self._served += 1
            self._sem.release()

    def note_served(self) -> None:
        """Count a request served outside :meth:`acquire` (e.g. a cache hit)."""
        with self._lock:
            self._served += 1

    def update(self, *, max_concurrency: int | None = None, min_interval_sec: float | None = None) -> None:
        """Hot-apply new limits; the semaphore is replaced, in-flight slots finish."""
        if max_concurrency is not None:
            value = max(1, int(max_concurrency))
            if value != self.max_concurrency:
                self.max_concurrency = value
                self._sem = threading.BoundedSemaphore(value)
        if min_interval_sec is not None:
            self.min_interval_sec = max(0.0, float(min_interval_sec))

    def stats(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "inflight": self._inflight,
                "served": self._served,
                "rejected": self._rejected,
                "max_concurrency": self.max_concurrency,
                "min_interval": self.min_interval_sec,
            }

    # ----------------------------------------------------------------- helpers
    def _acquire_slot(self, cancel_event: threading.Event | None) -> bool:
        while True:
            if self._sem.acquire(timeout=POLL):
                break
            if cancel_event is not None and cancel_event.is_set():
                return False

        admitted = True
        try:
            while self.min_interval_sec > 0:
                with self._lock:
                    now = time.monotonic()
                    wait = self._next_start - now
                    if wait <= 0:
                        self._next_start = now + self.min_interval_sec
                        break
                if cancel_event is not None and cancel_event.is_set():
                    admitted = False
                    break
                time.sleep(min(wait, POLL))
        finally:
            if not admitted:
                self._sem.release()
        return admitted
