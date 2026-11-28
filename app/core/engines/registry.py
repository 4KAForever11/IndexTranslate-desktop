"""Engine registry: id → engine instance.

Small on purpose. The pipeline asks for an engine by id (which travels inside
:class:`~app.core.models.TranslateRequest`), so a new provider is registered here
and becomes selectable everywhere without touching the UI.
"""

from __future__ import annotations

from .base import BaseEngine


class EngineRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, BaseEngine] = {}

    def register(self, engine: BaseEngine) -> BaseEngine:
        """Add (or replace) an engine; returns it so calls can be chained."""
        self._engines[engine.engine_id] = engine
        return engine

    def get(self, engine_id: str) -> BaseEngine:
        try:
            return self._engines[engine_id]
        except KeyError:
            raise KeyError(f"未注册的翻译引擎: {engine_id}") from None

    def list_engines(self) -> list[BaseEngine]:
        return list(self._engines.values())

    def ids(self) -> list[str]:
        return list(self._engines)

    def __contains__(self, engine_id: object) -> bool:
        return engine_id in self._engines

    def __len__(self) -> int:
        return len(self._engines)
