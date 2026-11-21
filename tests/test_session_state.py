import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.domain.budget import estimate_tokens, state_json_text, turns_tokens
from src.domain.session import (
    SessionSlots,
    TopicSessionState,
    apply_extraction,
    ceiling_check,
    commit_turn,
    is_idle_expired,
)

T0 = datetime(2026, 11, 21, 3, 0, tzinfo=UTC)


def _fresh(**overrides: object) -> TopicSessionState:
    fields: dict[str, object] = {
        "id": "s1",
        "topic_type": "general",
        "turn_count": 0,
        "extraction_count": 0,
        "state_json": {},
        "recent_turns": (),
        "last_active_at": T0,
    }
    fields.update(overrides)
    return TopicSessionState.model_validate(fields)


def test_dod_c_trace_extracts_once_on_u4_and_force_closes_on_u7() -> None:
    state = _fresh()
    for n in (1, 2, 3):
        assert ceiling_check(state, f"U{n}") == "ok"
        state = commit_turn(state, f"U{n}", f"A{n}", T0)
    assert len(state.recent_turns) == 6

    assert ceiling_check(state, "U4") == "extract"
    state = apply_extraction(state, SessionSlots(topic="翡翠", facts=("A1", "A2")))
    assert state.recent_turns == ()
    assert state.extraction_count == 1
    state = commit_turn(state, "U4", "A4", T0)

    for n in (5, 6):
        assert ceiling_check(state, f"U{n}") == "ok"
        state = commit_turn(state, f"U{n}", f"A{n}", T0)
    assert ceiling_check(state, "U7") == "force_close"


def test_three_full_size_exchanges_fill_history_to_exactly_1500_tokens() -> None:
    state = _fresh()
    for _ in range(3):
        assert ceiling_check(state, "字" * 2000) == "ok"
        state = commit_turn(state, "字" * 2000, "字" * 2000, T0)
    assert turns_tokens(state.recent_turns) <= 1500
    assert ceiling_check(state, "x") == "extract"


def test_invariant_holds_after_every_commit_over_random_lengths() -> None:
    rng = random.Random(7)
    state = _fresh()
    for _ in range(50):
        user, assistant = "u" * rng.randint(0, 3000), "a" * rng.randint(0, 3000)
        verdict = ceiling_check(state, user)
        if verdict == "force_close":
            state = _fresh()
        elif verdict == "extract":
            state = apply_extraction(state, SessionSlots(topic="t"))
        state = commit_turn(state, user, assistant, T0)
        assert len(state.recent_turns) <= 6
        assert turns_tokens(state.recent_turns) <= 1500
        assert state.extraction_count <= 1


def test_oversized_assistant_reply_is_cut_to_250_tokens() -> None:
    state = commit_turn(_fresh(), "hi", "長" * 5000, T0)
    assert estimate_tokens(state.recent_turns[1].text) <= 250
    assert state.recent_turns[1].text.startswith("長")


def test_extracted_slots_fit_state_json_and_keep_the_layer2_reference() -> None:
    state = _fresh(state_json={"last_artifact_id": "k" * 32, "workflow": "sales_copy"})
    slots = SessionSlots(
        topic="珠" * 100, facts=tuple("事" * 100 for _ in range(5)), pending_request='"q"' * 50
    )
    after = apply_extraction(state, slots)
    assert estimate_tokens(state_json_text(after.state_json)) <= 250
    assert after.state_json["last_artifact_id"] == "k" * 32
    assert after.state_json["workflow"] == "sales_copy"
    assert after.state_json["slots"].startswith('{"topic":"珠')


def test_small_slots_are_stored_whole() -> None:
    after = apply_extraction(_fresh(), SessionSlots(topic="翡翠", facts=("綠色",)))
    assert json.loads(after.state_json["slots"]) == {
        "topic": "翡翠",
        "facts": ["綠色"],
        "pending_request": None,
    }


def test_slots_schema_is_fixed() -> None:
    with pytest.raises(ValidationError):
        SessionSlots.model_validate({"topic": "t", "extra": 1})
    with pytest.raises(ValidationError):
        SessionSlots.model_validate({"topic": "t", "facts": ["a"] * 6})


def test_extraction_count_cannot_exceed_one() -> None:
    with pytest.raises(ValidationError):
        _fresh(extraction_count=2)


def test_idle_boundary_is_15_minutes_inclusive() -> None:
    state = _fresh()
    assert not is_idle_expired(state, T0 + timedelta(minutes=14, seconds=59))
    assert is_idle_expired(state, T0 + timedelta(minutes=15))


def test_commit_moves_last_active_and_counts_turns() -> None:
    later = T0 + timedelta(minutes=3)
    state = commit_turn(_fresh(), "hi", "hello", later)
    assert state.last_active_at == later
    assert state.turn_count == 1


def test_extraction_terminates_when_the_layer2_reference_leaves_no_room_for_slots() -> None:
    near_full = {"last_artifact_id": "k" * 32, "workflow": "w" * 139}
    assert estimate_tokens(state_json_text(near_full)) <= 250
    after = apply_extraction(_fresh(state_json=near_full), SessionSlots(topic="翡翠"))
    assert estimate_tokens(state_json_text(after.state_json)) <= 250
    assert after.state_json["workflow"] == "w" * 139
    assert after.recent_turns == ()
    assert after.extraction_count == 1
