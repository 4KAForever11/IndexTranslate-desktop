"""The one place a translation actually happens.

Every surface (main page, selection popup, screenshot overlay, batch queue, and
later the PDF/epub importer) calls :meth:`TranslationPipeline.execute_stream`.
The pipeline owns the cross-cutting concerns that used to be copy-pasted per
feature:

1. **glossary** — match the source text against the enabled term groups and
   inject only what actually occurs;
2. **cache** — identical text + parameters replay instantly;
3. **rate limiting** — bounded concurrency plus a minimum interval, so a batch
   queue cannot stampede the free endpoint;
4. **fallback** — on an overload, walk ``35B → 9B → 2B`` (configurable).

The fallback rule is deliberately conservative: a retry with the next model is
allowed **only while nothing has been emitted**. If the first model already
streamed half a sentence, switching models would splice two translations
together, so the error is raised and the user keeps the partial text.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Iterator

from .cache_db import TranslationCacheDB, make_key
from .config import DEFAULT_MODEL, data_dir, settings
from .engines import EngineRegistry, default_registry
from .glossary_db import GlossaryDB
from .models import (
    ALL_GROUPS,
    EngineError,
    RetryableEngineError,
    TranslateChunk,
    TranslateRequest,
)
from .rate_limiter import RateLimiter

#: Model walk used when the primary model answers 429/5xx.
DEFAULT_FALLBACK_MODELS = ("Index-Translate-9B", "Index-Translate-2B")


class TranslationPipeline:
    def __init__(
        self,
        registry: EngineRegistry | None = None,
        glossary_db: GlossaryDB | None = None,
        cache_db: TranslationCacheDB | None = None,
        rate_limiter: RateLimiter | None = None,
        fallback_chain: list[tuple[str, str]] | None = None,
    ) -> None:
        self.registry = registry or default_registry()
        self.glossary = glossary_db
        self.cache = cache_db
        self.limiter = rate_limiter or RateLimiter()
        self.fallback_chain: list[tuple[str, str]] = list(fallback_chain or [])
        self.cache_enabled = True
        self.cache_ttl_days = 7.0
        self.cache_max_rows = 10000

    # ------------------------------------------------------------------ wiring
    def set_glossary(self, glossary_db: GlossaryDB | None) -> None:
        self.glossary = glossary_db

    def set_cache(self, cache_db: TranslationCacheDB | None) -> None:
        self.cache = cache_db

    def reload_settings(self) -> None:
        """Re-read the limits and the fallback list from user settings."""
        self.cache_enabled = bool(settings.get("cache_enabled", True))
        self.cache_ttl_days = float(settings.get("cache_ttl_days", 7) or 7)
        self.cache_max_rows = int(settings.get("cache_max_rows", 10000) or 0)
        self.limiter.update(
            max_concurrency=int(settings.get("rate_max_concurrency", 3) or 3),
            min_interval_sec=float(settings.get("rate_min_interval", 0.25) or 0.0),
        )
        if bool(settings.get("fallback_enabled", True)):
            models = settings.get("fallback_models", list(DEFAULT_FALLBACK_MODELS))
            if isinstance(models, str):
                models = [part.strip() for part in models.replace("，", ",").split(",")]
            self.fallback_chain = [
                ("index_translate", str(model).strip())
                for model in (models or [])
                if str(model).strip() and str(model).strip() != DEFAULT_MODEL
            ]
        else:
            self.fallback_chain = []
        if self.cache is not None:
            self.cache.ttl_sec = max(0.0, self.cache_ttl_days * 86400.0)
            self.cache.max_rows = self.cache_max_rows

    def shutdown(self) -> None:
        if self.cache is not None:
            self.cache.close()
            self.cache = None

    # ------------------------------------------------------------------ helpers
    def _ensure_cache(self) -> TranslationCacheDB | None:
        if self.cache is None and self.cache_enabled:
            try:
                self.cache = TranslationCacheDB(
                    data_dir() / "translation_cache.db",
                    ttl_sec=max(0.0, self.cache_ttl_days * 86400.0),
                    max_rows=self.cache_max_rows,
                )
            except Exception:
                self.cache = None
        return self.cache

    def _resolve_terms(self, req: TranslateRequest) -> list[str]:
        """The ``term→translation`` strings this request should carry."""
        if self.glossary is None:
            return []
        if req.glossary_group is None or not bool(req.extra.get("use_glossary", True)):
            return []
        text = (req.text or "").strip()
        if text:
            pairs = self.glossary.match_terms(text, req.glossary_group)
        elif req.is_image:
            # An image has no source text to scan; hand over the enabled list.
            pairs = self.glossary.all_enabled()
        else:
            pairs = []
        return [f"{term}→{translation}" for term, translation in pairs if translation]

    def _candidates(self, req: TranslateRequest) -> list[tuple[object, str]]:
        wanted: list[tuple[str, str]] = [(req.engine_id, req.model)]
        wanted.extend(self.fallback_chain)

        out: list[tuple[object, str]] = []
        seen: set[tuple[str, str]] = set()
        for engine_id, model in wanted:
            model = (model or req.model or DEFAULT_MODEL).strip()
            key = (engine_id, model)
            if key in seen:
                continue
            try:
                engine = self.registry.get(engine_id)
            except KeyError:
                continue
            if req.is_image and not engine.capabilities.supports_image:
                continue
            seen.add(key)
            out.append((engine, model))
        return out

    # --------------------------------------------------------------------- run
    def execute_stream(
        self,
        req: TranslateRequest,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[TranslateChunk]:
        cancel = cancel_event or threading.Event()
        if cancel.is_set():
            return

        terms = self._resolve_terms(req)
        cache_key = ""

        if req.use_cache and self.cache_enabled and not req.is_image:
            cache = self._ensure_cache()
            if cache is not None:
                cache_key = make_key(
                    req.text,
                    req.source_lang,
                    req.target_lang,
                    req.model,
                    req.instruction,
                    terms,
                )
                hit = cache.get(cache_key)
                if hit is not None and hit[0].strip():
                    text, engine_used, model_used = hit
                    self.limiter.note_served()
                    yield TranslateChunk(
                        delta=text,
                        done=True,
                        engine_used=engine_used or req.engine_id,
                        model_used=model_used or req.model,
                        from_cache=True,
                        matched_terms=list(terms),
                    )
                    return

        candidates = self._candidates(req)
        if not candidates:
            raise EngineError(
                f"没有可用的翻译引擎：{req.engine_id}",
                "请在「设置」里检查该引擎的配置。",
            )

        parts: list[str] = []
        last_error: EngineError | None = None

        for index, (engine, model) in enumerate(candidates):
            if cancel.is_set():
                return
            attempt_req = req if model == req.model else replace(req, model=model)
            with self.limiter.acquire(cancel) as admitted:
                if not admitted:
                    return
                try:
                    for chunk in engine.translate_stream(attempt_req, terms, cancel):  # type: ignore[attr-defined]
                        if chunk.delta:
                            parts.append(chunk.delta)
                        yield TranslateChunk(
                            delta=chunk.delta,
                            done=chunk.done,
                            engine_used=engine.engine_id,  # type: ignore[attr-defined]
                            model_used=model,
                            from_cache=False,
                            matched_terms=list(terms),
                        )
                except RetryableEngineError as exc:
                    last_error = exc
                    partial = "".join(parts).strip()
                    is_last = index >= len(candidates) - 1
                    if partial or is_last:
                        raise
                    continue
                # Completed normally: remember the answer for next time.
                if cache_key and parts and not cancel.is_set():
                    cache = self._ensure_cache()
                    if cache is not None:
                        cache.set(cache_key, "".join(parts), engine.engine_id, model)  # type: ignore[attr-defined]
                return

        raise last_error or EngineError("翻译请求失败，请稍后重试。")


# --------------------------------------------------------------- module singleton
_PIPELINE: TranslationPipeline | None = None
_LOCK = threading.Lock()


def get_pipeline() -> TranslationPipeline:
    """Process-wide pipeline (workers, popup and batch share one rate limiter)."""
    global _PIPELINE
    with _LOCK:
        if _PIPELINE is None:
            _PIPELINE = TranslationPipeline()
            _PIPELINE.reload_settings()
        return _PIPELINE


def configure_pipeline(
    glossary_db: GlossaryDB | None = None,
    cache_db: TranslationCacheDB | None = None,
    *,
    reload: bool = True,
) -> TranslationPipeline:
    """Attach the real glossary/cache and refresh limits from settings."""
    pipeline = get_pipeline()
    if glossary_db is not None:
        pipeline.set_glossary(glossary_db)
    if cache_db is not None:
        pipeline.set_cache(cache_db)
    if reload:
        pipeline.reload_settings()
    return pipeline
