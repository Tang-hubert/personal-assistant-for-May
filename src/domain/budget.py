"""Prompt budget law (ARCHITECTURE §4.1-§4.2, ADR-0003). Pure: no I/O.

Every slot limit is a validator, so an over-budget prompt cannot be constructed. Builders
truncate first, then construct.
"""

from __future__ import annotations

import json
import math
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.domain.media import MAX_BATCH_IMAGES

TEXT_TOTAL = 4500
SLOT_SYSTEM = 500
SLOT_STATE = 250
SLOT_RETRIEVED = 1200
SLOT_TURNS = 1500
SLOT_USER_MSG = 1050
MULTIMODAL_TOTAL = 7500
MAX_IMAGES = MAX_BATCH_IMAGES
MAX_AUDIO_S = 120
FREE_TEXT_MAX_CHARS = 850
TURN_MAX_TOKENS = 250
MAX_TURNS = 6
IMAGE_TOKENS = 560  # Gemini MEDIA_RESOLUTION_MEDIUM
AUDIO_TOKENS_PER_S = 32

ELLIPSIS = "…"


def estimate_tokens(text: str) -> int:
    """ceil(len(text) * 1.2), in integer arithmetic so float rounding can never add a token."""
    return -(-len(text) * 6 // 5)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Deterministic prefix cut; the result, ellipsis included, estimates to <= max_tokens."""
    if estimate_tokens(text) <= max_tokens:
        return text
    if max_tokens <= 0:
        return ""
    max_chars = max_tokens * 5 // 6
    if max_chars < len(ELLIPSIS) + 1:
        return text[:max_chars]
    return text[: max_chars - len(ELLIPSIS)] + ELLIPSIS


def truncate_free_text(text: str) -> tuple[str, bool]:
    """Cut free text to 850 chars (850 * 1.2 = 1,020 <= 1,050). True when something was cut."""
    return text[:FREE_TEXT_MAX_CHARS], len(text) > FREE_TEXT_MAX_CHARS


def state_json_text(state: dict[str, Any]) -> str:
    """The one serialization of state_json, used both to budget it and to put it in a prompt."""
    return json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_within(text: str, limit: int, slot: str) -> str:
    if estimate_tokens(text) > limit:
        raise ValueError(f"{slot} is {estimate_tokens(text)} tokens, over its {limit} budget")
    return text


def turns_tokens(turns: tuple[Turn, ...]) -> int:
    return sum(estimate_tokens(turn.text) for turn in turns)


def check_history(turns: tuple[Turn, ...]) -> None:
    """ADR-0003 invariant 1: <= 6 messages and <= 1,500 tokens."""
    if len(turns) > MAX_TURNS:
        raise ValueError(f"{len(turns)} recent turns, over the {MAX_TURNS} limit")
    if turns_tokens(turns) > SLOT_TURNS:
        raise ValueError(f"recent turns are {turns_tokens(turns)} tokens, over {SLOT_TURNS}")


def check_state_json(state: dict[str, Any]) -> None:
    _require_within(state_json_text(state), SLOT_STATE, "state_json")


class _Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Turn(_Value):
    role: Literal["user", "assistant"]
    text: str

    @field_validator("text")
    @classmethod
    def _cap(cls, text: str) -> str:
        return _require_within(text, TURN_MAX_TOKENS, "turn")


class PromptParts(_Value):
    """A text prompt that fits the 4,500-token row by construction, counted as sent: the
    [state]/[context] headers system_text() adds are billed too."""

    system: str
    state_json: dict[str, Any] = Field(default_factory=dict)
    retrieved_context: str = ""
    recent_turns: tuple[Turn, ...] = ()
    current_user_msg: str

    @model_validator(mode="after")
    def _check_slots(self) -> Self:
        _require_within(self.system, SLOT_SYSTEM, "system")
        check_state_json(self.state_json)
        _require_within(self.retrieved_context, SLOT_RETRIEVED, "retrieved_context")
        check_history(self.recent_turns)
        _require_within(self.current_user_msg, SLOT_USER_MSG, "current_user_msg")
        if self.text_tokens() > TEXT_TOTAL:
            raise ValueError(f"prompt is {self.text_tokens()} tokens as sent, over {TEXT_TOTAL}")
        return self

    def text_tokens(self) -> int:
        return (
            estimate_tokens(self.system_text())
            + turns_tokens(self.recent_turns)
            + estimate_tokens(self.current_user_msg)
        )

    def system_text(self) -> str:
        """System instruction plus the state and retrieved-context slots, as providers see it."""
        sections = [self.system]
        if self.state_json:
            sections.append(f"[state]\n{state_json_text(self.state_json)}")
        if self.retrieved_context:
            sections.append(f"[context]\n{self.retrieved_context}")
        return "\n\n".join(sections)


def multimodal_estimate(parts: PromptParts, n_images: int, audio_s: float | None) -> int:
    audio = math.ceil(audio_s * AUDIO_TOKENS_PER_S) if audio_s else 0
    return parts.text_tokens() + n_images * IMAGE_TOKENS + audio
