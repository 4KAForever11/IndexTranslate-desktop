"""轻量界面文案表（JSON 词表，不引入 Qt Linguist）。

设计取舍：只有「界面框架」这类高频、短小、结构稳定的文案进词表；提示语、
错误说明这些带上下文的长句子仍然写中文——半翻译比不翻译更难维护。
缺 key 时回落到简中，再回落到 key 本身，绝不会显示空白。
"""
from __future__ import annotations

import json
from pathlib import Path

LANGUAGE_DIR = Path(__file__).resolve().parent.parent / "locales"
DEFAULT_LANG = "zh_CN"
LANGUAGES: tuple[tuple[str, str], ...] = (
    ("zh_CN", "简体中文"),
    ("zh_TW", "繁體中文"),
    ("en_US", "English"),
)

_current = DEFAULT_LANG
_strings: dict[str, str] = {}
_fallback: dict[str, str] = {}


def _load_file(lang: str) -> dict[str, str]:
    path = LANGUAGE_DIR / f"{lang}.json"
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def available() -> tuple[tuple[str, str], ...]:
    return LANGUAGES


def current() -> str:
    return _current


def load(lang: str | None = None) -> str:
    """切换语言；返回真正生效的语言代码。"""
    global _current, _strings, _fallback
    code = (lang or DEFAULT_LANG).strip()
    known = {item[0] for item in LANGUAGES}
    if code not in known:
        code = DEFAULT_LANG
    if not _fallback:
        _fallback = _load_file(DEFAULT_LANG)
    _strings = _load_file(code) if code != DEFAULT_LANG else dict(_fallback)
    _current = code
    return code


def t(key: str, default: str | None = None, **kwargs: object) -> str:
    """取一条文案；``{name}`` 形式的占位符会被 kwargs 填充。"""
    if not _strings:
        load(_current)
    text = _strings.get(key) or _fallback.get(key) or default or key.rsplit(".", 1)[-1]
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return text