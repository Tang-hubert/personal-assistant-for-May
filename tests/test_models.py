from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from src.domain.media import Intent, encode_callback, intent_prompt, parse_callback
from src.domain.models import (
    ActionButton,
    BotResponse,
    CopyableBlock,
    IncomingEvent,
    ReplyTarget,
)


def _event(**overrides: Any) -> IncomingEvent:
    fields: dict[str, Any] = {
        "event_id": "u-1",
        "tenant_id": "t-1",
        "user_id": "p-1",
        "channel": "telegram",
        "event_type": "text",
        "reply_target": ReplyTarget(address="12345"),
        "payload_text": "翡翠放哪裡",
    }
    fields.update(overrides)
    return IncomingEvent(**fields)


def test_bot_response_title_limit() -> None:
    BotResponse(title="字" * 30)
    with pytest.raises(ValidationError):
        BotResponse(title="字" * 31)


def test_bot_response_at_most_three_bullets() -> None:
    BotResponse(title="t", bullets=("a", "b", "c"))
    with pytest.raises(ValidationError):
        BotResponse(title="t", bullets=("a", "b", "c", "d"))


def test_bot_response_at_most_four_actions() -> None:
    button = ActionButton(label="好", callback_data="b:1:lecture")
    BotResponse(title="t", actions=(button,) * 4)
    with pytest.raises(ValidationError):
        BotResponse(title="t", actions=(button,) * 5)


def test_action_button_label_limit() -> None:
    ActionButton(label="字" * 20, callback_data="x")
    with pytest.raises(ValidationError):
        ActionButton(label="字" * 21, callback_data="x")


def test_callback_data_limit_counts_utf8_bytes() -> None:
    ActionButton(label="ok", callback_data="a" * 64)
    with pytest.raises(ValidationError):
        ActionButton(label="ok", callback_data="a" * 65)
    # 22 CJK chars = 66 bytes in UTF-8, over Telegram's 64-byte limit.
    with pytest.raises(ValidationError):
        ActionButton(label="ok", callback_data="翠" * 22)


def test_copyable_block_body_limit() -> None:
    CopyableBlock(label="A 版", body_text="字" * 1000)
    with pytest.raises(ValidationError):
        CopyableBlock(label="A 版", body_text="字" * 1001)


def test_alt_text_is_derived_and_truncated_to_400() -> None:
    response = BotResponse(title="講座重點", bullets=("第一點", "第二點"))
    assert response.alt_text == "講座重點\n第一點\n第二點"

    long_response = BotResponse(title="t" * 30, bullets=("字" * 300, "字" * 300))
    assert len(long_response.alt_text) == 400


def test_alt_text_is_not_hand_written() -> None:
    with pytest.raises(ValidationError):
        BotResponse(title="t", alt_text="custom")  # type: ignore[call-arg]


def test_alt_text_appears_in_serialized_output() -> None:
    assert BotResponse(title="t").model_dump()["alt_text"] == "t"


def test_group_index_and_total_come_together_and_are_ordered() -> None:
    _event(event_type="image", asset_ids=("a1",), group_index=2, group_total=3)
    with pytest.raises(ValidationError):
        _event(event_type="image", asset_ids=("a1",), group_index=2)
    with pytest.raises(ValidationError):
        _event(event_type="image", asset_ids=("a1",), group_index=4, group_total=3)
    with pytest.raises(ValidationError):
        _event(event_type="image", asset_ids=("a1",), group_index=0, group_total=3)


@pytest.mark.parametrize(
    ("event_type", "extra"), [("image", {}), ("audio", {"audio_duration_s": 3.0})]
)
def test_media_events_require_assets(event_type: str, extra: dict[str, Any]) -> None:
    _event(event_type=event_type, asset_ids=("a1",), **extra)
    with pytest.raises(ValidationError):
        _event(event_type=event_type, asset_ids=(), **extra)


def test_audio_events_require_a_positive_duration() -> None:
    _event(event_type="audio", asset_ids=("a1",), audio_duration_s=121.0)
    with pytest.raises(ValidationError):
        _event(event_type="audio", asset_ids=("a1",))
    with pytest.raises(ValidationError):
        _event(event_type="audio", asset_ids=("a1",), audio_duration_s=0)


def test_callback_events_require_callback_data() -> None:
    _event(event_type="action_callback", callback_data="b:1:lecture", payload_text=None)
    with pytest.raises(ValidationError):
        _event(event_type="action_callback", payload_text=None)


def test_unsupported_event_needs_no_payload() -> None:
    _event(event_type="unsupported", payload_text=None)


def test_reply_target_rejects_naive_expiry() -> None:
    with pytest.raises(ValidationError):
        ReplyTarget(address="U1", ephemeral_token="r", token_expires_at=datetime(2026, 1, 1))


def test_models_are_immutable() -> None:
    response = BotResponse(title="t")
    with pytest.raises(ValidationError):
        response.title = "changed"  # type: ignore[misc]


def test_intent_buttons_fit_the_channel_limits() -> None:
    response = intent_prompt("a" * 32, kept_count=4)
    assert len(response.actions) == 3
    assert all(len(a.label) <= 20 for a in response.actions)
    assert all(len(a.callback_data.encode()) <= 64 for a in response.actions)
    assert all("a" * 32 in a.callback_data for a in response.actions)


def test_callback_round_trips_and_rejects_junk() -> None:
    for intent in Intent:
        assert parse_callback(encode_callback("b" * 32, intent)) == ("b" * 32, intent)
    for junk in ("", "x:1:lecture", "b:abc", "b:abc:unknown", "b::lecture", "b:a:b:lecture"):
        assert parse_callback(junk) is None
