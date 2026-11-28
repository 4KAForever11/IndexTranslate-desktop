"""OpenAI-compatible engine (Ollama, DeepSeek, SiliconFlow, LM Studio…).

Only the pieces that differ from Index-Translate are overridden: no
``chat_template_kwargs`` (a generic server rejects unknown fields), an optional
``Authorization: Bearer`` header, and a user-supplied base URL. Everything else
— streaming, cancellation, retries — is inherited, which is exactly the point of
having the pipeline speak one protocol.
"""

from __future__ import annotations

from typing import Any

from ..http_client import build_chat_payload
from ..models import EngineCapabilities, EngineError, TranslateRequest
from .index_translate import IndexTranslateEngine


class OpenAICompatEngine(IndexTranslateEngine):
    """Any server that implements ``POST {base_url}/chat/completions``."""

    engine_id = "openai_compat"
    display_name = "OpenAI 兼容接口"
    capabilities = EngineCapabilities(supports_stream=True, supports_image=True)

    def __init__(self, base_url: str = "", api_key: str = "", timeout: float = 120.0) -> None:
        super().__init__(base_url=base_url, timeout=timeout)
        self.api_key = api_key

    def _base_url(self, req: TranslateRequest) -> str:
        base = str(req.extra.get("api_base") or self.base_url or "").strip().rstrip("/")
        if not base:
            raise EngineError(
                "未配置 OpenAI 兼容接口地址。",
                "请在「设置 → 代理优化 → 降级服务」里填写 base_url（例如 http://127.0.0.1:11434/v1）。",
            )
        return base

    def _headers(self, req: TranslateRequest) -> dict[str, str]:
        headers = super()._headers(req)
        key = str(req.extra.get("api_key") or self.api_key or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _payload(
        self,
        req: TranslateRequest,
        content: str | list[dict[str, Any]],
        *,
        stream: bool,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        return build_chat_payload(
            req.model,
            content,
            stream=stream,
            max_tokens=max_tokens,
            temperature=temperature,
            native=False,
        )
