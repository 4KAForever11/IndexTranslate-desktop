"""Language table — byte-identical to the one used by call_api.py."""

from __future__ import annotations

LANG_NAMES: dict[str, str] = {
    "en": "英语", "zh": "中文", "de": "德语", "fr": "法语", "es": "西班牙语",
    "ja": "日语", "ko": "韩语", "pt": "葡萄牙语", "ru": "俄语", "ar": "阿拉伯语",
    "it": "意大利语", "nl": "荷兰语", "pl": "波兰语", "ro": "罗马尼亚语",
    "sv": "瑞典语", "tr": "土耳其语", "hi": "印地语", "vi": "越南语",
    "th": "泰语", "id": "印尼语", "ms": "马来语", "fil": "菲律宾语",
    "ukr_cyrl": "乌克兰语", "fas_arab": "波斯语", "ces_latn": "捷克语",
    "ell_grek": "希腊语", "dan_latn": "丹麦语", "hun_latn": "匈牙利语",
    "fin_latn": "芬兰语", "nob_latn": "书面挪威语", "slk_latn": "斯洛伐克语",
    "bul_cyrl": "保加利亚语",
}

# Languages most users touch first; everything else follows in table order.
_PRIORITY = ["zh", "en", "ja", "ko", "fr", "de", "es", "ru", "pt", "it", "ar"]

AUTO_LABEL = "自动检测"


def target_languages() -> list[tuple[str, str]]:
    """(code, display name) for the target-language picker."""
    ordered = [code for code in _PRIORITY if code in LANG_NAMES]
    ordered += [code for code in LANG_NAMES if code not in ordered]
    return [(code, LANG_NAMES[code]) for code in ordered]


def source_languages() -> list[tuple[str, str]]:
    return [("auto", AUTO_LABEL)] + target_languages()


def display_name(code: str) -> str:
    return AUTO_LABEL if code == "auto" else LANG_NAMES.get(code, code)


def code_for_name(name: str) -> str:
    for code, label in LANG_NAMES.items():
        if label == name:
            return code
    return "en"
