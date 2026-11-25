"""Prompt construction — ported 1:1 from call_api.py.

Do not "improve" the wording here: the public model was trained against these
prompts, and the constraints/JSON-preservation rules are part of the contract.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .languages import LANG_NAMES


def parse_glossary_terms(glossary_input: str) -> list[str]:
    """Parse a glossary given as a JSON file path, inline JSON, or delimited pairs."""
    if not glossary_input:
        return []
    glossary_input = glossary_input.strip()

    # 1. Path of an existing JSON file: {"term": "target"}
    if os.path.isfile(glossary_input):
        try:
            with open(glossary_input, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return [f"{k.strip()}→{str(v).strip()}" for k, v in data.items()]
        except Exception:
            pass

    # 2. Inline JSON object
    if glossary_input.startswith("{") and glossary_input.endswith("}"):
        try:
            data = json.loads(glossary_input)
            if isinstance(data, dict):
                return [f"{k.strip()}→{str(v).strip()}" for k, v in data.items()]
        except Exception:
            pass

    # 3. Delimited pairs: "k:v", "k：v", "k->v", "k→v", separated by , or ，
    pairs: list[str] = []
    for item in glossary_input.replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        for sep in ("->", "→", ":", "："):
            if sep in item:
                key, value = item.split(sep, 1)
                pairs.append(f"{key.strip()}→{value.strip()}")
                break
        else:
            pairs.append(item)
    return pairs


def _source_name(source_lang: str) -> str:
    if not source_lang or source_lang.lower() in ("auto", ""):
        return ""
    return LANG_NAMES.get(source_lang.lower(), source_lang)


def _target_name(target_lang: str) -> str:
    name = LANG_NAMES.get(target_lang.lower())
    if name is None:
        raise ValueError(f"unknown target language code: {target_lang}")
    return name


def build_prompt(
    text: str,
    target_lang: str,
    source_lang: str = "auto",
    instruction: str = "",
    glossary: str = "",
    genre: str = "文本",
) -> str:
    """Canonical Index-Translate instTrans prompt (text mode)."""
    target_name = _target_name(target_lang)
    source_name = _source_name(source_lang)

    constraints: list[str] = []
    if glossary:
        terms = parse_glossary_terms(glossary)
        if terms:
            constraints.append(f"【硬性要求】专名/术语对照: {'、'.join(terms)}")

    if instruction:
        for line in instruction.strip().splitlines():
            line = line.strip().lstrip("0123456789. ")
            if not line:
                continue
            if line.startswith(("【硬性要求】", "【注意】", "【软性要求】")):
                constraints.append(line)
            else:
                constraints.append(f"【注意】{line}")

    stripped = text.strip()
    if constraints:
        header_src = f"{source_name}{genre}" if source_name else genre
        header = f"请将以下{header_src}翻译成{target_name}，并且严格遵循所有约束要求。"
        requirement_lines = "\n".join(
            f"{index + 1}. {constraint}" for index, constraint in enumerate(constraints)
        )

        is_json = False
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                is_json = isinstance(json.loads(stripped), dict)
            except Exception:
                is_json = False

        if is_json and any(("JSON" in c or "json" in c) for c in constraints):
            suffix = (
                "请以相同的 JSON 格式输出翻译结果，key 保持不变，value 为对应译文。"
                "只输出 JSON，不要有任何额外说明。"
            )
        else:
            suffix = "只输出译文，不要有任何额外说明。"

        return (
            f"{header}\n\n"
            f"【源文】\n"
            f"{stripped}\n\n"
            f"【约束要求】\n"
            f"{requirement_lines}\n\n"
            f"{suffix}"
        )

    if source_name:
        return f"请将以下{source_name}文本翻译为{target_name}，直接输出翻译结果，不要进行任何解释。\n\n{stripped}"
    return f"请将以下文本翻译为{target_name}，直接输出翻译结果，不要进行任何解释。\n\n{stripped}"


def build_image_prompt(
    data_url: str,
    target_lang: str,
    source_lang: str = "auto",
    ref_text: str = "",
    instruction: str = "",
    glossary: str = "",
) -> list[dict[str, Any]]:
    """Multimodal prompt: [text, image_url] content array (image mode)."""
    target_name = _target_name(target_lang)
    source_name = _source_name(source_lang)

    base = (
        f"请将图片中的{source_name}文本翻译为{target_name}，直接输出翻译结果，不要进行任何解释。"
        if source_name
        else f"请将图片中的文本翻译为{target_name}，直接输出翻译结果，不要进行任何解释。"
    )

    extras: list[str] = []
    if ref_text:
        extras.append(f"【参考文本/关注内容】\n{ref_text}")
    if instruction:
        extras.append(f"【指令要求】{instruction}")
    if glossary:
        terms = parse_glossary_terms(glossary)
        if terms:
            extras.append(f"【术语对照】{'、'.join(terms)}")

    prompt_text = (
        f"{base}\n\n" + "\n\n".join(extras) + "\n\n只输出翻译结果，不要进行任何解释。"
        if extras
        else base
    )

    return [
        {"type": "text", "text": prompt_text},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]


def strip_think(text: str) -> str:
    """Strip CoT blocks — the marker is `</think>`, exactly as in call_api.py."""
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip().removeprefix("<think>").strip()
