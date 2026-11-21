import math
from fractions import Fraction
from typing import Any

import pytest
from pydantic import ValidationError

from src.domain.budget import (
    MULTIMODAL_TOTAL,
    TEXT_TOTAL,
    PromptParts,
    Turn,
    estimate_tokens,
    multimodal_estimate,
    state_json_text,
    truncate_free_text,
    truncate_to_tokens,
)
from src.domain.models import TaskCapability
from src.domain.ports import LLMRequest


def _chars_for(tokens: int) -> int:
    """Longest text whose estimate is still <= tokens."""
    return tokens * 5 // 6


@pytest.mark.parametrize(("length", "expected"), [(0, 0), (1, 2), (5, 6), (850, 1020)])
def test_estimator_is_ceil_of_len_times_1_2(length: int, expected: int) -> None:
    assert estimate_tokens("字" * length) == expected


def test_estimator_matches_exact_rational_ceiling_for_every_length() -> None:
    for length in range(3000):
        assert estimate_tokens("a" * length) == math.ceil(Fraction(length) * Fraction(6, 5))


@pytest.mark.parametrize("max_tokens", [0, 1, 2, 7, 250, 1050])
def test_truncate_to_tokens_never_exceeds_the_limit(max_tokens: int) -> None:
    for length in (0, 1, 50, 208, 209, 875, 876, 4000):
        text = "珠" * length
        cut = truncate_to_tokens(text, max_tokens)
        assert estimate_tokens(cut) <= max_tokens
        assert cut == truncate_to_tokens(text, max_tokens)


def test_truncate_to_tokens_keeps_text_that_already_fits() -> None:
    text = "a" * _chars_for(250)
    assert truncate_to_tokens(text, 250) == text


def test_truncate_to_tokens_keeps_the_prefix_and_marks_the_cut() -> None:
    cut = truncate_to_tokens("abcdefghij" * 100, 12)
    assert cut == "abcdefghi…"


def test_free_text_boundary_at_850_chars() -> None:
    assert truncate_free_text("字" * 850) == ("字" * 850, False)
    assert truncate_free_text("字" * 851) == ("字" * 850, True)


def test_turn_rejects_more_than_250_tokens() -> None:
    Turn(role="user", text="a" * _chars_for(250))
    with pytest.raises(ValidationError):
        Turn(role="user", text="a" * (_chars_for(250) + 1))


def _full_parts(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "system": "s" * _chars_for(500),
        "state_json": {},
        "retrieved_context": "r" * _chars_for(1200),
        "recent_turns": tuple(
            Turn(role="user" if i % 2 == 0 else "assistant", text="t" * _chars_for(250))
            for i in range(6)
        ),
        "current_user_msg": "u" * _chars_for(1050),
    }
    fields.update(overrides)
    return fields


def test_prompt_parts_at_every_slot_ceiling_fit_the_text_total() -> None:
    parts = PromptParts(**_full_parts())
    total = (
        estimate_tokens(parts.system)
        + estimate_tokens(state_json_text(parts.state_json))
        + estimate_tokens(parts.retrieved_context)
        + sum(estimate_tokens(turn.text) for turn in parts.recent_turns)
        + estimate_tokens(parts.current_user_msg)
    )
    assert total <= TEXT_TOTAL


def _wire_tokens(parts: PromptParts) -> int:
    """What a provider receives: the system instruction with its slots, each turn, the message."""
    return (
        estimate_tokens(parts.system_text())
        + sum(estimate_tokens(turn.text) for turn in parts.recent_turns)
        + estimate_tokens(parts.current_user_msg)
    )


def _state_of(tokens: int) -> dict[str, str]:
    state = {"k": "v" * tokens}
    while estimate_tokens(state_json_text(state)) > tokens:
        state = {"k": state["k"][:-1]}
    return state


def test_prompt_with_every_slot_at_its_cap_is_rejected_for_its_wire_headers() -> None:
    full = _full_parts(state_json=_state_of(250))
    assert estimate_tokens(state_json_text(full["state_json"])) == 250
    with pytest.raises(ValidationError, match="over 4500"):
        PromptParts(**full)


def test_text_tokens_counts_exactly_what_providers_receive() -> None:
    parts = PromptParts(
        system="系統",
        state_json={"workflow": "sales_copy"},
        retrieved_context="上次",
        current_user_msg="嗨",
    )
    assert parts.text_tokens() == _wire_tokens(parts)


@pytest.mark.parametrize("state_tokens", [0, 20, 100, 200, 250])
@pytest.mark.parametrize("retrieved_cut", [0, 1, 10, 30, 300])
def test_wire_text_of_any_constructible_prompt_fits_the_text_total(
    state_tokens: int, retrieved_cut: int
) -> None:
    fields = _full_parts(
        state_json=_state_of(state_tokens) if state_tokens else {},
        retrieved_context="r" * (_chars_for(1200) - retrieved_cut),
    )
    try:
        parts = PromptParts(**fields)
    except ValidationError:
        return
    assert _wire_tokens(parts) <= TEXT_TOTAL


@pytest.mark.parametrize(
    ("slot", "limit"),
    [("system", 500), ("retrieved_context", 1200), ("current_user_msg", 1050)],
)
def test_prompt_parts_reject_a_text_slot_one_token_over(slot: str, limit: int) -> None:
    over = "x" * (_chars_for(limit) + 1)
    assert estimate_tokens(over) == limit + 1 or estimate_tokens(over) > limit
    with pytest.raises(ValidationError):
        PromptParts(**_full_parts(**{slot: over}))


def test_prompt_parts_reject_state_json_over_250_tokens() -> None:
    fits = {"k": "v" * 190}
    assert estimate_tokens(state_json_text(fits)) <= 250
    PromptParts(system="", current_user_msg="", state_json=fits)
    with pytest.raises(ValidationError):
        PromptParts(system="", current_user_msg="", state_json={"k": "v" * 210})


def test_prompt_parts_reject_a_seventh_turn() -> None:
    turns = tuple(Turn(role="user", text="hi") for _ in range(7))
    with pytest.raises(ValidationError):
        PromptParts(**_full_parts(recent_turns=turns))


def test_multimodal_estimate_counts_images_and_audio_seconds() -> None:
    parts = PromptParts(system="", current_user_msg="")
    assert multimodal_estimate(parts, n_images=6, audio_s=None) == 6 * 560
    assert multimodal_estimate(parts, n_images=0, audio_s=120) == 120 * 32


def test_architect_worst_case_fits_the_multimodal_total() -> None:
    parts = PromptParts(system="s" * _chars_for(500), current_user_msg="u" * _chars_for(1050))
    estimate = multimodal_estimate(parts, n_images=3, audio_s=120)
    assert estimate == 3 * 560 + 120 * 32 + 500 + 1050 == 7070
    assert estimate <= MULTIMODAL_TOTAL == 7500


def _request(parts: PromptParts, **overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "tenant_id": "t",
        "user_id": "u",
        "workflow_name": "test",
        "capability": TaskCapability.MULTIMODAL_IMAGE,
        "prompt": parts,
        "max_output_tokens": 100,
    }
    fields.update(overrides)
    return LLMRequest(**fields)


def test_request_allows_six_images_and_rejects_seven() -> None:
    parts = PromptParts(system="", current_user_msg="")
    _request(parts, asset_ids=tuple(f"a{i}" for i in range(6)))
    with pytest.raises(ValidationError):
        _request(parts, asset_ids=tuple(f"a{i}" for i in range(7)))


def test_request_allows_120_seconds_of_audio_and_rejects_121() -> None:
    parts = PromptParts(system="", current_user_msg="")
    audio = {"capability": TaskCapability.MULTIMODAL_AUDIO, "asset_ids": ("v1",)}
    _request(parts, audio_duration_s=120, **audio)
    with pytest.raises(ValidationError):
        _request(parts, audio_duration_s=121, **audio)


def test_request_at_architect_worst_case_is_accepted() -> None:
    parts = PromptParts(system="s" * _chars_for(500), current_user_msg="u" * _chars_for(1050))
    _request(parts, asset_ids=("v1", "i1", "i2", "i3"), audio_duration_s=120)


def test_request_over_the_multimodal_total_cannot_be_built() -> None:
    parts = PromptParts(
        system="s" * _chars_for(500),
        retrieved_context="r" * _chars_for(1200),
        current_user_msg="u" * _chars_for(1050),
    )
    assert multimodal_estimate(parts, n_images=3, audio_s=120) > MULTIMODAL_TOTAL
    with pytest.raises(ValidationError, match="over 7500"):
        _request(parts, asset_ids=("v1", "i1", "i2", "i3"), audio_duration_s=120)


def test_truncate_to_a_non_positive_budget_is_empty() -> None:
    assert truncate_to_tokens("abcdef", 0) == ""
    assert truncate_to_tokens("abcdef", -1) == ""
