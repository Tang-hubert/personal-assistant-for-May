"""Deterministic Taiwan direct-sales claim scanner (ARCHITECTURE §6). No LLM involved.

The term dictionary ships inside the package next to this module, so it is part of the image
and never shadowed by the data/ volume.
"""

from __future__ import annotations

import re
import unicodedata
from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_TERMS_PATH = Path(__file__).with_name("tw_prohibited_terms.yaml")
SANITIZE_ROUNDS = 3
WARNING_MAX_CHARS = 200
_GAP = r"(?:[^\w]|_)*"  # what normalize() strips between characters: spaces, punctuation, symbols


class _Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProhibitedTerm(_Value):
    term: str = Field(min_length=1)
    category: str
    law: str
    safe: str | None = None


class ComplianceHit(_Value):
    term: str
    category: str
    law: str


class _Entry(_Value):
    term: str = Field(min_length=1)
    safe: str | None = None


class _Category(_Value):
    law: str = Field(min_length=1)
    terms: tuple[_Entry, ...] = Field(min_length=1)


class _Dictionary(_Value):
    version: int
    categories: dict[str, _Category] = Field(min_length=1)


@cache
def load_terms(path: Path = DEFAULT_TERMS_PATH) -> tuple[ProhibitedTerm, ...]:
    """Longest term first, so a longer claim is handled before a term it contains."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    parsed = _Dictionary.model_validate(raw)
    terms = [
        ProhibitedTerm(term=e.term, category=name, law=cat.law, safe=e.safe)
        for name, cat in parsed.categories.items()
        for e in cat.terms
    ]
    return tuple(sorted(terms, key=lambda t: len(normalize(t.term)), reverse=True))


def normalize(text: str) -> str:
    """NFKC, casefold, keep letters and digits only: spaced or punctuated spellings still match."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def scan(text: str, terms: tuple[ProhibitedTerm, ...] | None = None) -> tuple[ComplianceHit, ...]:
    flat = normalize(text)
    found: list[tuple[int, ComplianceHit]] = []
    for t in terms if terms is not None else load_terms():
        pos = flat.find(normalize(t.term))
        if pos >= 0:
            found.append((pos, ComplianceHit(term=t.term, category=t.category, law=t.law)))
    return tuple(hit for _, hit in sorted(found, key=lambda pair: pair[0]))


def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(_GAP.join(re.escape(ch) for ch in normalize(term)), re.IGNORECASE)


def _replace_pass(text: str, terms: tuple[ProhibitedTerm, ...]) -> str:
    for t in terms:
        text = _pattern(t.term).sub(t.safe or "", text)
    return text


def sanitize(text: str, terms: tuple[ProhibitedTerm, ...] | None = None) -> str:
    """Return text with 0 hits, never raising. Up to SANITIZE_ROUNDS replacement passes (a
    replacement can join its neighbours into a new term); then any survivor is deleted from the
    NFKC form one match at a time, which always ends because every deletion shortens the text."""
    terms = terms if terms is not None else load_terms()
    for _ in range(SANITIZE_ROUNDS):
        if not scan(text, terms):
            return text
        text = _replace_pass(text, terms)
    if not scan(text, terms):
        return text
    text = unicodedata.normalize("NFKC", text)
    while hits := scan(text, terms):
        target = next(t for t in terms if t.term == hits[0].term)
        text, n = _pattern(target.term).subn("", text, count=1)
        if n == 0:  # the match exists only after casefolding letters; drop to the flat form
            text = normalize(text).replace(normalize(target.term), "", 1)
    return text


def legal_warning(hits: tuple[ComplianceHit, ...]) -> str:
    terms = "、".join(dict.fromkeys(h.term for h in hits))
    laws = "；".join(dict.fromkeys(h.law for h in hits))
    text = f"⚠️ 這版含有「{terms}」等說法，可能違反{laws}。公開貼文前請改用 A 版。"
    if len(text) <= WARNING_MAX_CHARS:
        return text
    return text[: WARNING_MAX_CHARS - 1] + "…"
