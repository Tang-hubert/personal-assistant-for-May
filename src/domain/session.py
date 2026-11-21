"""Layer-2 topic session state machine (ARCHITECTURE §4.3, ADR-0003). Pure: no I/O, no clock.

TopicSessionState checks the ADR-0003 invariants on construction, so every transition that
returns a state has already proved them.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.domain.budget import (
    MAX_TURNS,
    SLOT_STATE,
    SLOT_TURNS,
    TURN_MAX_TOKENS,
    Turn,
    check_history,
    check_state_json,
    estimate_tokens,
    state_json_text,
    truncate_to_tokens,
    turns_tokens,
)

TopicType = Literal["lecture", "sales", "memory", "ios_help", "general"]
CeilingVerdict = Literal["ok", "extract", "force_close"]

IDLE_TIMEOUT = timedelta(minutes=15)
SLOTS_KEY = "slots"
ARTIFACT_KEY = "last_artifact_id"  # Layer-2 reference to a knowledge_items row (ADR-0003)
WORKFLOW_KEY = "workflow"


MAX_FACTS = 5


class SessionSlots(BaseModel):
    """The single slot extraction's fixed schema. Build it from LLM text with parse_slots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = ""
    facts: tuple[str, ...] = Field(default=(), max_length=MAX_FACTS)
    pending_request: str | None = None


def parse_slots(text: str) -> SessionSlots:
    """Lenient parse of the extraction reply. The session gets one extraction, so a sixth fact
    or an extra key must not discard the rest; empty only when the text is not a JSON object."""
    try:
        data = json.loads(text)
    except ValueError:
        return SessionSlots()
    if not isinstance(data, dict):
        return SessionSlots()
    topic = data.get("topic")
    facts = data.get("facts")
    if isinstance(facts, str):
        facts = [facts]
    pending = data.get("pending_request")
    return SessionSlots(
        topic="" if topic is None else str(topic),
        facts=tuple(str(fact) for fact in facts[:MAX_FACTS]) if isinstance(facts, list) else (),
        pending_request=None if pending is None else str(pending),
    )


class TopicSessionState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    topic_type: TopicType
    turn_count: int = Field(ge=0)
    extraction_count: int = Field(ge=0, le=1)
    state_json: dict[str, Any]
    recent_turns: tuple[Turn, ...]
    last_active_at: AwareDatetime

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        check_history(self.recent_turns)
        check_state_json(self.state_json)
        return self

    def evolve(self, **changes: Any) -> TopicSessionState:
        """A validated copy; model_copy would skip the invariant check."""
        return TopicSessionState.model_validate({**dict(self), **changes})


def is_idle_expired(state: TopicSessionState, now: datetime) -> bool:
    return now >= state.last_active_at + IDLE_TIMEOUT


def ceiling_check(state: TopicSessionState, incoming_user_text: str) -> CeilingVerdict:
    """Predict the history after this turn commits; the reply is counted at its 250 cap."""
    user_tokens = estimate_tokens(truncate_to_tokens(incoming_user_text, TURN_MAX_TOKENS))
    count = len(state.recent_turns) + 2
    tokens = turns_tokens(state.recent_turns) + user_tokens + TURN_MAX_TOKENS
    if count <= MAX_TURNS and tokens <= SLOT_TURNS:
        return "ok"
    return "extract" if state.extraction_count == 0 else "force_close"


def apply_extraction(state: TopicSessionState, slots: SessionSlots) -> TopicSessionState:
    """Store the slots as truncated JSON text beside the Layer-2 reference; drop every turn."""
    base = {k: v for k, v in state.state_json.items() if k != SLOTS_KEY}
    serialized = slots.model_dump_json()
    budget = SLOT_STATE
    candidate = {**base, SLOTS_KEY: truncate_to_tokens(serialized, budget)}
    # JSON escaping makes the stored text longer than the cut, so shrink until it all fits.
    while estimate_tokens(state_json_text(candidate)) > SLOT_STATE and budget > 0:
        budget -= 1
        candidate = {**base, SLOTS_KEY: truncate_to_tokens(serialized, budget)}
    if estimate_tokens(state_json_text(candidate)) > SLOT_STATE:
        candidate = base  # the Layer-2 reference alone fills the slot; the slots are dropped
    return state.evolve(state_json=candidate, recent_turns=(), extraction_count=1)


def commit_turn(
    state: TopicSessionState, user_text: str, assistant_text: str, now: datetime
) -> TopicSessionState:
    turns = (
        *state.recent_turns,
        Turn(role="user", text=truncate_to_tokens(user_text, TURN_MAX_TOKENS)),
        Turn(role="assistant", text=truncate_to_tokens(assistant_text, TURN_MAX_TOKENS)),
    )
    return state.evolve(recent_turns=turns, turn_count=state.turn_count + 1, last_active_at=now)
