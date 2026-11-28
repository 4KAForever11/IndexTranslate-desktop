"""占位符守卫：把代码、标签、变量在送翻之前换成哨兵，翻完再换回来。

模型很爱干这三件事：把 `{user_name}` 翻成 `{用户名}`、把 `npm run build` 改坏、
把 `<b>` 吞掉。光靠提示词约束不可靠，所以在程序侧做硬保护，并在还原时校验，
漏掉的哨兵作为「需人工复核」提示返回给界面。
"""

from __future__ import annotations

import re

# 按优先级保护：行内代码 -> HTML 标签 -> 双花括号 -> 单花括号变量 -> printf 占位符
PROTECT_PATTERN = re.compile(
    r"(`[^`\n]+`|</?[a-zA-Z][^>]*>|\{\{[^{}]+\}\}|\{[a-zA-Z0-9_.\-]+\}|%(?:\([a-zA-Z0-9_]+\))?[sdif])"
)

TOKEN_TEMPLATE = "⟦P{n}⟧"
_TOKEN_RE = re.compile(r"⟦P\d+⟧")

HINT = "保留原文中所有 ⟦P0⟧、⟦P1⟧ 形式的占位符原样输出，不要翻译或增删。"


class PlaceholderGuard:
    """保护 / 还原成对使用；一个实例可以处理多个文本。"""

    def protect(self, text: str) -> tuple[str, dict[str, str]]:
        mapping: dict[str, str] = {}

        def _repl(match: re.Match[str]) -> str:
            token = TOKEN_TEMPLATE.format(n=len(mapping))
            mapping[token] = match.group(0)
            return token

        protected = PROTECT_PATTERN.sub(_repl, text) if text else text
        return protected, mapping

    def restore(self, translated: str, mapping: dict[str, str]) -> tuple[str, list[str]]:
        """还原占位符，返回被模型漏掉/篡改的原始片段列表。"""
        if not mapping:
            return translated, []
        result = translated or ""
        missing: list[str] = []
        for token, raw in mapping.items():
            if token not in result:
                missing.append(raw)
                continue
            result = result.replace(token, raw)
        # 模型可能自己又造了 ⟦P9⟧ 这种不存在的哨兵，一并清掉，避免污染译文
        leftovers = _TOKEN_RE.findall(result)
        for token in leftovers:
            if token not in mapping:
                result = result.replace(token, "")
        return result, missing

    # ------------------------------------------------------------------ utils
    @staticmethod
    def hint_for(mapping: dict[str, str]) -> str:
        return HINT if mapping else ""

    @staticmethod
    def has_tokens(text: str) -> bool:
        return bool(_TOKEN_RE.search(text or ""))


Guard = PlaceholderGuard()


def protect_batch(texts: list[str]) -> list[tuple[str, dict[str, str]]]:
    """给一组文本各自做保护（哨兵编号互不冲突）。"""
    guard = PlaceholderGuard()
    return [guard.protect(text) for text in texts]
