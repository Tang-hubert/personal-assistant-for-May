from pathlib import Path

import pytest
from pydantic import ValidationError

from src.domain import compliance
from src.domain.compliance import (
    DEFAULT_TERMS_PATH,
    ProhibitedTerm,
    legal_warning,
    load_terms,
    sanitize,
    scan,
)


def test_dictionary_loads_with_category_and_law_for_every_term() -> None:
    terms = load_terms()
    assert len(terms) >= 30
    assert all(t.category and t.law for t in terms)


def test_dictionary_ships_with_the_code_not_under_the_data_volume() -> None:
    assert DEFAULT_TERMS_PATH.parent == Path(compliance.__file__).parent
    assert "data" not in DEFAULT_TERMS_PATH.parts
    assert DEFAULT_TERMS_PATH.is_file()


def test_malformed_dictionary_fails_loudly(tmp_path: Path) -> None:
    bad = tmp_path / "terms.yaml"
    bad.write_text("version: 1\ncategories:\n  x:\n    terms: []\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_terms(bad)


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("每天戴著可以治療關節痛", "治療"),
        ("這條手鍊保證有效", "保證有效"),
        ("治 療效果很好", "治療"),
        ("保．證有效喔", "保證有效"),
        ("治★病磁場超強", "治病磁場"),
    ],
)
def test_scan_catches_terms_even_when_split_by_spaces_or_punctuation(text: str, term: str) -> None:
    assert term in {hit.term for hit in scan(text)}


def test_clean_copy_has_no_hits() -> None:
    assert scan("這條翡翠手鍊色澤溫潤，戴起來很有氣質，送給媽媽最貼心。") == ()


def test_sanitize_always_ends_with_zero_hits() -> None:
    dirty = "戴著能治療失眠、降血壓，保證有效，還有治病磁場，穩賺不賠！"
    clean = sanitize(dirty)
    assert scan(clean) == ()
    assert "保養" in clean


def test_longer_term_is_handled_before_a_term_it_contains() -> None:
    clean = sanitize("這顆水晶有治病磁場")
    assert clean == "這顆水晶有"


def test_replacement_that_forms_a_new_term_still_converges() -> None:
    terms = (
        ProhibitedTerm(term="甲乙", category="c", law="L", safe="乙"),
        ProhibitedTerm(term="乙丙", category="c", law="L", safe="甲"),
    )
    for text in ("甲甲乙丙", "甲乙丙乙丙甲乙", "甲 乙丙"):
        assert scan(sanitize(text, terms), terms) == ()


def test_every_safe_replacement_is_itself_clean() -> None:
    for term in load_terms():
        if term.safe is not None:
            assert scan(term.safe) == (), term


def test_sanitize_keeps_full_width_punctuation_of_clean_text() -> None:
    text = "溫潤，有氣質。"
    assert sanitize(text) == text


def test_legal_warning_lists_terms_and_laws_within_200_chars() -> None:
    hits = scan("可以治療失眠又保證有效")
    warning = legal_warning(hits)
    assert "治療" in warning and "保證有效" in warning
    assert "藥事法第69條" in warning and "公平交易法第21條" in warning
    assert len(warning) <= 200
    assert len(legal_warning(scan("治療" * 3 + "".join(t.term for t in load_terms())))) <= 200
