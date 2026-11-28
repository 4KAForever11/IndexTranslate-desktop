"""Translation engines and the registry that owns them."""

from __future__ import annotations

from .base import BaseEngine
from .index_translate import IndexTranslateEngine
from .openai_compat import OpenAICompatEngine
from .registry import EngineRegistry

__all__ = [
    "BaseEngine",
    "EngineRegistry",
    "IndexTranslateEngine",
    "OpenAICompatEngine",
    "default_registry",
]


def default_registry() -> EngineRegistry:
    """Registry with the built-in engines.

    ``openai_compat`` is registered even when unconfigured: the pipeline filters
    candidates by capability, and the engine raises a clear message if a caller
    selects it without a base URL.
    """
    registry = EngineRegistry()
    registry.register(IndexTranslateEngine())
    registry.register(OpenAICompatEngine())
    return registry
