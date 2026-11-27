"""Pure helpers shared by the local proxy: cache keys, glossary injection, SSE replay.

Kept free of Qt and of the HTTP server so they can be unit-tested directly.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Iterable, Sequence

# Fields that change the answer; everything else (stream flags, user ids …) must
# NOT be part of the key or the cache would never hit.
_KEY_FIELDS = (
    "model",
    "messages",
    "temperature",
    "top_p",
    "max_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "response_format",
)

GLOSSARY_HEADER = "【硬性要求】专名/术语对照:"
MAX_GLOSSARY_TERMS = 40


# --------------------------------------------------------------------- cache
def make_cache_key(payload: dict[str, Any]) -> str:
    """A stable hash of everything that influences the translation."""
    picked = {name: payload.get(name) for name in _KEY_FIELDS if name in payload}
    blob = json.dumps(picked, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------- reading
def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        ]
        return "\n".join(parts)
    return ""


def extract_source_text(messages: Any) -> str:
    """The text a glossary lookup should scan (user turns first, else all)."""
    if not isinstance(messages, list):
        return ""
    user = [
        _message_text(msg.get("content"))
        for msg in messages
        if isinstance(msg, dict) and msg.get("role") == "user"
    ]
    text = "\n".join(part for part in user if part)
    if text:
        return text
    return "\n".join(
        _message_text(msg.get("content")) for msg in messages if isinstance(msg, dict)
    )


# ------------------------------------------------------------------ glossary
def match_glossary(text: str, pairs: Iterable[tuple[str, str]]) -> list[str]:
    """Terms from ``pairs`` that actually occur in ``text``, longest first."""
    if not text:
        return []
    lowered = text.lower()
    hits: list[str] = []
    for term, translation in pairs:
        term = (term or "").strip()
        if not term or not (translation or "").strip():
            continue
        if term.lower() in lowered:
            hits.append(f"{term}→{translation.strip()}")
    # Longest terms first so "Transformer 架构" survives next to "Transformer".
    hits.sort(key=len, reverse=True)
    return hits[:MAX_GLOSSARY_TERMS]


def inject_glossary(payload: dict[str, Any], pairs: Sequence[tuple[str, str]]) -> int:
    """Append matched terms to the last user message; returns how many were added.

    Mutates ``payload`` in place (it is the dict we are about to forward) and
    returns 0 when nothing matched, so the caller can skip the work entirely.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return 0
    text = extract_source_text(messages)
    hits = match_glossary(text, pairs)
    if not hits:
        return 0
    block = f"\n\n{GLOSSARY_HEADER} {'、'.join(hits)}"

    # Prefer the last user turn; fall back to the last message of any role.
    target = None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            target = msg
            break
    if target is None:
        target = messages[-1] if isinstance(messages[-1], dict) else None
    if target is None:
        return 0

    content = target.get("content")
    if isinstance(content, str):
        target["content"] = content + block
    elif isinstance(content, list):
        for part in reversed(content):
            if isinstance(part, dict) and part.get("type") in (None, "text"):
                part["text"] = str(part.get("text", "")) + block
                break
        else:
            content.append({"type": "text", "text": block.strip()})
    else:
        target["content"] = block.strip()
    return len(hits)


# --------------------------------------------------------------------- model
def model_chain(primary: str, extras: str, all_models: Iterable[str]) -> list[str]:
    """The order of models to try, best first.

    ``primary`` always leads. When ``extras`` names fallbacks explicitly those are
    used verbatim (so the user stays in control); when it is empty we walk the
    built-in ladder from the primary downwards, which is what "35B → 9B → 2B"
    degradation means in practice.
    """
    available = [str(item) for item in all_models]
    known = set(available)
    first = (primary or "").strip()
    requested = [
        name.strip()
        for name in str(extras or "").replace("，", ",").split(",")
        if name.strip()
    ]

    chain: list[str] = []
    if first:
        chain.append(first)
    if requested:
        chain.extend(name for name in requested if name in known and name not in chain)
    elif first in known:
        chain.extend(name for name in available[available.index(first) + 1:] if name not in chain)
    return chain


# ------------------------------------------------------------------ response
def _chunk_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def completion_json(text: str, model: str) -> dict[str, Any]:
    """A non-streaming OpenAI ``chat.completion`` body."""
    return {
        "id": _chunk_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def sse_from_text(text: str, model: str, chunk_size: int = 160) -> bytes:
    """Rebuild an SSE stream from a cached string.

    Immersive Translate and friends only care about ``delta.content`` and the
    terminating ``data: [DONE]``, so a synthetic stream is indistinguishable
    from the real one — it just arrives much faster.
    """
    ident = _chunk_id()
    created = int(time.time())
    out: list[str] = []

    def emit(delta: dict[str, Any], finish: str | None = None) -> None:
        body = {
            "id": ident,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        out.append(f"data: {json.dumps(body, ensure_ascii=False)}\n\n")

    emit({"role": "assistant", "content": ""})
    for start in range(0, len(text), chunk_size):
        emit({"content": text[start : start + chunk_size]})
    emit({"content": ""}, "stop")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode("utf-8")


def extract_stream_text(raw: bytes) -> str:
    """Sum the ``delta.content`` of a buffered SSE stream."""
    pieces: list[str] = []
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            pieces.append(delta["content"])
    return "".join(pieces)


def extract_json_text(raw: bytes) -> str:
    """Pull ``choices[0].message.content`` out of a buffered JSON response."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""
