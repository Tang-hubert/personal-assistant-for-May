"""Portable CJK search tokens (ADR-0004).

CJK runs become character bigrams; other words are lowercased.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff"  # kana, CJK, hangul
_RUN = re.compile(rf"(?P<cjk>[{_CJK}]+)|(?P<word>(?:(?![{_CJK}])[^\W_])+)")


def _tokens(text: str) -> Iterable[str]:
    for match in _RUN.finditer(text):
        if cjk := match.group("cjk"):
            if len(cjk) == 1:
                yield cjk
            else:
                yield from (cjk[i : i + 2] for i in range(len(cjk) - 1))
        else:
            yield match.group("word").lower()


def _unique(tokens: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(tokens))


def build_search_tokens(text: str, keywords: Iterable[str]) -> str:
    return " ".join(_unique([*_tokens(text), *(t for kw in keywords for t in _tokens(kw))]))


def build_match_query(query: str) -> str | None:
    """FTS5 MATCH expression: every token quoted and OR-ed, so user text is never FTS syntax."""
    tokens = _unique(_tokens(query))
    if not tokens:
        return None
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
