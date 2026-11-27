"""风格预设：把「可选指令」变成一排可点的芯片。

每个预设就是一段会被当作 ``instruction`` 发给模型的中文要求，因此与
``prompt_builder`` 的约束模式完全兼容 —— 不会影响默认对齐测试，因为默认
预设 "默认直译" 的 ``text`` 是空串，等价于不附加任何指令。

预设本身存在 ``settings["styles"]`` 里，用户可增删改；``DEFAULT_STYLES`` 是
首次启动的种子，也是「恢复默认」时要回到的样子。
"""

from __future__ import annotations

import copy
from typing import Any

DEFAULT_STYLES: list[dict[str, str]] = [
    {
        "id": "plain",
        "name": "默认直译",
        "text": "",
    },
    {
        "id": "natural",
        "name": "地道意译",
        "text": (
            "译文要地道自然，符合目标语言母语者的表达习惯；可以调整语序、合并或拆分句子，"
            "但不要改变原意，也不要增删信息。"
        ),
    },
    {
        "id": "tech",
        "name": "技术文档",
        "text": (
            "这是技术文档：代码、命令、变量名、函数名、API 名称、文件路径、版本号一律保持原样不翻译；"
            "术语使用业界通用译法；保留原有的 Markdown 结构与列表层级。"
        ),
    },
    {
        "id": "academic",
        "name": "学术润色",
        "text": "使用正式、严谨的学术语体，避免口语化与缩略表达，专业术语前后一致，句式完整。",
    },
    {
        "id": "acg",
        "name": "口语 / ACG",
        "text": (
            "使用轻松口语化的语气，贴近日常对话或字幕口吻；ACG、游戏语境下的专有名词、"
            "角色名、招式名可以保留原文或使用社区通用译名。"
        ),
    },
]

MAX_STYLES = 24
MAX_STYLE_NAME = 12
MAX_STYLE_TEXT = 400


def default_styles() -> list[dict[str, str]]:
    """A fresh, mutable copy of the built-in presets."""
    return copy.deepcopy(DEFAULT_STYLES)


def _clean(item: Any, *, fallback_id: str = "") -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    style_id = str(item.get("id") or "").strip() or fallback_id or name
    return {
        "id": style_id,
        "name": name[:MAX_STYLE_NAME],
        "text": str(item.get("text") or "").strip()[:MAX_STYLE_TEXT],
    }


def load_styles(raw: Any) -> list[dict[str, str]]:
    """Normalise whatever is in settings into a usable preset list.

    Anything unusable falls back to the built-ins, so a hand-edited settings
    file can never leave the UI with an empty chip row.
    """
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for index, entry in enumerate(raw):
            cleaned = _clean(entry, fallback_id=f"style-{index}")
            if cleaned is None or cleaned["id"] in seen:
                continue
            seen.add(cleaned["id"])
            items.append(cleaned)
    if not items:
        return default_styles()
    return items[:MAX_STYLES]


def find_style(styles: list[dict[str, str]], style_id: str) -> dict[str, str] | None:
    for style in styles:
        if style["id"] == style_id:
            return style
    return None


def style_text(styles: list[dict[str, str]], style_id: str) -> str:
    """The instruction text for a chip; unknown/empty ids mean "no instruction"."""
    style = find_style(styles, style_id)
    return style["text"] if style else ""


def new_style_id(existing: list[dict[str, str]]) -> str:
    used = {style["id"] for style in existing}
    index = 1
    while f"custom-{index}" in used:
        index += 1
    return f"custom-{index}"
