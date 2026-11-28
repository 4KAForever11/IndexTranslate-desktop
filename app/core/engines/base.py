"""Engine interface.

An *engine* is a chat-completions endpoint plus the request shape it needs
(forced ``enable_thinking``, Bearer auth, model naming). Everything above it —
cache, rate limiting, glossary injection, fallback — lives in the pipeline, so
adding a provider is a small, self-contained class.

The ``cancel_event`` is part of the signature rather than an afterthought: the
app keeps the partial translation when the user presses 停止, so an engine must
be able to stop *between* tokens instead of being killed mid-flight.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Iterator

from ..models import EngineCapabilities, TranslateChunk, TranslateRequest


class BaseEngine(ABC):
    """One translation backend."""

    engine_id: str = "base"
    display_name: str = "Base"
    capabilities: EngineCapabilities = EngineCapabilities()

    @abstractmethod
    def translate_stream(
        self,
        req: TranslateRequest,
        matched_terms: list[str],
        cancel_event: threading.Event,
    ) -> Iterator[TranslateChunk]:
        """Stream the translation of ``req``.

        ``matched_terms`` are ``"term→translation"`` strings the caller decided
        to inject; an engine that cannot use them may ignore the argument.
        Implementations must yield at least one chunk with ``done=True`` on a
        successful run, and raise :class:`~app.core.models.EngineError` (or the
        retryable subclass) on failure.
        """
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} engine_id={self.engine_id!r}>"
