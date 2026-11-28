"""Unified data protocol for the translation core (V2 foundation).

Everything that wants a translation — the main page, the selection popup, the
screenshot overlay, the batch queue and the local proxy's own calls — speaks
these types instead of passing a dozen loose keyword arguments around. Engines
consume :class:`TranslateRequest` and produce :class:`TranslateChunk` streams;
the pipeline in :mod:`app.core.pipeline` decides *which* engine/model runs and
whether the answer comes from the cache.

Why a dedicated error hierarchy: the pipeline has to tell "this model is
overloaded, try the next one" apart from "this request is invalid, stop
immediately". ``status_code`` is kept for logging and for turning the failure
back into an HTTP error in the proxy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Literal

from .config import DEFAULT_MODEL

# Which surface asked for the translation. Kept coarse on purpose: it drives
# logging and future per-scene defaults, not control flow.
SceneType = Literal["translate", "proxy", "popup", "document", "polish"]

#: ``"*"`` means "every enabled glossary group"; ``None`` disables injection.
ALL_GROUPS = "*"


class EngineError(Exception):
    """A translation failure that can be shown to the user as-is."""

    def __init__(self, message: str, detail: str = "", status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.status_code = status_code

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.message


class RetryableEngineError(EngineError):
    """Overload / transient transport failure: another model may still work."""

    def __init__(self, message: str, status_code: int | None = None, detail: str = "") -> None:
        super().__init__(message, detail, status_code)


@dataclass(frozen=True)
class EngineCapabilities:
    """What an engine can do. The pipeline filters candidates with it."""

    supports_stream: bool = True
    supports_image: bool = False
    supports_instruction: bool = True
    supports_glossary: bool = True


@dataclass
class TranslateRequest:
    """One translation job, independent of who asked for it.

    ``image_b64`` accepts either a full ``data:image/...;base64,...`` URL or bare
    base64 payload — engines normalise it. ``glossary_group`` defaults to
    ``"*"`` (all enabled groups) because the desktop app has always injected the
    whole enabled glossary; a caller may pin one group by name, or pass ``None``
    to translate without any term injection.

    ``extra`` carries engine-specific knobs so the protocol does not grow a new
    field for every setting: ``api_base``, ``timeout``, ``temperature``,
    ``handle`` (:class:`app.core.http_client.ResponseHandle`) and
    ``use_glossary`` are understood by the built-in engines.
    """

    text: str = ""
    image_b64: str | None = None
    source_lang: str = "auto"
    target_lang: str = "zh"
    instruction: str = ""
    engine_id: str = "index_translate"
    model: str = DEFAULT_MODEL
    glossary_group: str | None = ALL_GROUPS
    use_cache: bool = True
    stream: bool = True
    scene: SceneType = "translate"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_image(self) -> bool:
        return bool(self.image_b64)


@dataclass
class TranslateChunk:
    """A streamed piece plus the provenance the UI needs to report.

    ``engine_used`` / ``model_used`` are filled in by the pipeline, so a chunk
    that arrived after a fallback can be labelled 「已自动降级至 9B」.
    """

    delta: str = ""
    done: bool = False
    engine_used: str = ""
    model_used: str = ""
    from_cache: bool = False
    matched_terms: list[str] = field(default_factory=list)


# Convenience alias for engine implementations.
ChunkStream = Iterator[TranslateChunk]
