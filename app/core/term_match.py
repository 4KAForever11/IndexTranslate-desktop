"""Term matching shared by the desktop app and the local proxy.

Naive substring matching is wrong in the direction that hurts most: a glossary
entry ``cat → 猫`` would fire on every *category*, injecting a translation the
text never asked for. So:

* terms made only of ASCII characters are matched with word boundaries
  (``cat`` matches ``a cat`` but not ``category``);
* terms containing CJK are matched as plain substrings, because Chinese has no
  word delimiters and ``光遇`` inside ``光遇国际服`` *should* hit.

Longest term first, so ``API 网关`` wins over ``API`` when both match.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

MAX_TERMS = 40


def is_ascii_term(term: str) -> bool:
    return bool(term) and all(ord(char) < 128 for char in term)


def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )


def term_hits(
    text: str,
    pairs: Iterable[tuple[str, str]],
    *,
    max_terms: int = MAX_TERMS,
) -> list[tuple[str, str]]:
    """Return the ``(term, translation)`` pairs that actually occur in ``text``."""
    if not (text or "").strip():
        return []
    folded = text.casefold()
    cached: dict[str, re.Pattern[str]] = {}
    hits: list[tuple[str, str]] = []

    for term, translation in pairs:
        term = (term or "").strip()
        if not term or not (translation or "").strip():
            continue
        if is_ascii_term(term):
            regex = cached.get(term)
            if regex is None:
                regex = cached[term] = _pattern(term)
            matched = regex.search(text) is not None
        else:
            matched = term.casefold() in folded
        if matched:
            hits.append((term, translation))

    hits.sort(key=lambda pair: (-len(pair[0]), pair[0]))
    return hits[: max(0, int(max_terms))]


def term_strings(pairs: Sequence[tuple[str, str]]) -> list[str]:
    """``[(term, translation)]`` → ``["term→translation"]`` (the prompt shape)."""
    return [f"{term}→{translation}" for term, translation in pairs]
