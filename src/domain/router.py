"""Deterministic-first router with the 3-layer bounded session law (ARCHITECTURE §4.3-§4.5).

Priority: photos go to the aggregator (zero-token, and their one reply comes from there, so the
budget gate cannot turn a burst into many replies: D1) -> budget at 100% answers the rest
message with no LLM call -> callbacks, unsupported events, and too long or too short voice
answer without an LLM -> free text pays for a DeepSeek classification; voice pays for a
transcription first and then takes the same path as typed text.

One user's events are handled one at a time under an in-process lock; the partial unique
index is what guarantees a single active session.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from src.domain import replies
from src.domain.budget import PromptParts, Turn, truncate_free_text
from src.domain.media import IntentSelected
from src.domain.models import BotResponse, IncomingEvent, TaskCapability
from src.domain.ports import (
    BudgetExhausted,
    BudgetGuardPort,
    BudgetStatus,
    Clock,
    LLMPort,
    LLMRequest,
    LLMUnavailable,
    SessionStorePort,
)
from src.domain.session import (
    ARTIFACT_KEY,
    SessionSlots,
    TopicSessionState,
    TopicType,
    apply_extraction,
    ceiling_check,
    commit_turn,
    is_idle_expired,
    parse_slots,
)

ROUTE_WORKFLOW = "route"
EXTRACT_WORKFLOW = "slot_extract"
ROUTE_MAX_OUTPUT = 64
EXTRACT_MAX_OUTPUT = 250
ROUTE_CONTEXT_TURNS = 2

ROUTE_SYSTEM = (
    "你是長輩助理的路由器。只輸出 JSON: "
    '{"route": "lecture|sales|memory|ios_help|general", "topic_shift": true|false}。'
    "lecture=講座筆記, sales=珠寶或商品文案, memory=記住或查詢東西放哪, "
    "ios_help=手機操作問題, general=其他。"
    "topic_shift 只有在使用者明顯換到和目前話題無關的新事情時才是 true。"
)
EXTRACT_SYSTEM = (
    "把這段對話整理成 JSON: "
    '{"topic": "主題", "facts": ["最多 5 條重點"], "pending_request": "還沒完成的請求或 null"}。'
    "只輸出 JSON, 用繁體中文, 越短越好。"
)


class RouteDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    route: TopicType
    topic_shift: bool = False


FALLBACK_DECISION = RouteDecision(route="general")


class TurnContext(BaseModel):
    """What a route handler gets: the truncated text and the session it may build a prompt
    from. The handler builds its own PromptParts and calls the LLMPort itself."""

    model_config = ConfigDict(frozen=True)

    event: IncomingEvent
    route: TopicType
    session: TopicSessionState
    user_text: str


class HandlerReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str  # committed to recent_turns as the assistant turn (truncated to 250 tokens)
    response: BotResponse
    last_artifact_id: str | None = None  # a rewrite's new knowledge item; later follow-ups use it


class RouteHandler(Protocol):
    async def __call__(self, ctx: TurnContext) -> HandlerReply: ...


class Transcriber(Protocol):
    async def __call__(self, event: IncomingEvent) -> str:
        """The voice clip as text; empty when nothing intelligible was said."""
        ...


class IntentHandler(Protocol):
    async def __call__(self, event: IncomingEvent, selected: IntentSelected) -> BotResponse: ...


class PhotoBatches(Protocol):
    async def add(self, event: IncomingEvent) -> None: ...

    async def handle_intent_callback(
        self, event: IncomingEvent
    ) -> IntentSelected | BotResponse: ...


class Router:
    def __init__(
        self,
        *,
        finops: BudgetGuardPort,
        llm: LLMPort,
        sessions: SessionStorePort,
        batches: PhotoBatches,
        handlers: Mapping[TopicType, RouteHandler],
        transcriber: Transcriber,
        intent_handler: IntentHandler,
        clock: Clock,
    ) -> None:
        if "general" not in handlers:
            raise ValueError("a 'general' handler is required as the fallback route")
        self._finops = finops
        self._llm = llm
        self._sessions = sessions
        self._batches = batches
        self._handlers = handlers
        self._transcriber = transcriber
        self._intent_handler = intent_handler
        self._clock = clock
        self._locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)

    async def handle(self, event: IncomingEvent) -> BotResponse | None:
        """At most one BotResponse per event (ADR-0002); None for photos, which the
        aggregator answers once per batch."""
        async with self._locks[(event.tenant_id, event.user_id)]:
            try:
                return await self._dispatch(event)
            except BudgetExhausted:
                return replies.rest_message()
            except LLMUnavailable:
                return replies.busy_message()

    async def _dispatch(self, event: IncomingEvent) -> BotResponse | None:
        if event.event_type == "image":
            await self._batches.add(event)
            return None
        if await self._finops.status(event.tenant_id) is BudgetStatus.EXHAUSTED:
            return replies.rest_message()
        match event.event_type:
            case "action_callback":
                return await self._callback(event)
            case "unsupported":
                return replies.unsupported_message()
            case "audio":
                if rejection := replies.voice_rejection(event.audio_duration_s or 0.0):
                    return rejection
                transcript = await self._transcriber(event)
                if not transcript.strip():
                    return replies.voice_unclear()
                return await self._text(event, transcript)
            case "text":
                return await self._text(event, event.payload_text or "")

    async def _callback(self, event: IncomingEvent) -> BotResponse:
        if event.callback_data == replies.FINISH_CALLBACK:
            active = await self._sessions.get_active(
                tenant_id=event.tenant_id, user_id=event.user_id
            )
            if active is not None:
                await self._sessions.close(
                    tenant_id=event.tenant_id, user_id=event.user_id, session_id=active.id
                )
            return replies.topic_finished()
        outcome = await self._batches.handle_intent_callback(event)
        if isinstance(outcome, BotResponse):
            return outcome
        return await self._intent_handler(event, outcome)

    async def _text(self, event: IncomingEvent, raw_text: str) -> BotResponse:
        tenant_id, user_id = event.tenant_id, event.user_id
        text, truncated = truncate_free_text(raw_text)
        now = self._clock.now()

        session = await self._sessions.get_active(tenant_id=tenant_id, user_id=user_id)
        if session is not None and is_idle_expired(session, now):
            await self._sessions.close(tenant_id=tenant_id, user_id=user_id, session_id=session.id)
            session = None

        decision = await self._classify(event, text, session)
        if session is None or decision.topic_shift:
            session = await self._sessions.open_fresh(
                tenant_id=tenant_id, user_id=user_id, topic_type=decision.route
            )

        verdict = ceiling_check(session, text)
        if verdict == "force_close":
            await self._sessions.close(tenant_id=tenant_id, user_id=user_id, session_id=session.id)
            return replies.new_topic_prompt()
        if verdict == "extract":
            session = apply_extraction(session, await self._extract(event, text, session))
            # Saved at once so a failing handler cannot lead to a second extraction.
            await self._sessions.save(tenant_id=tenant_id, user_id=user_id, state=session)

        route = decision.route
        if not decision.topic_shift and ARTIFACT_KEY in session.state_json:
            # A follow-up such as "改親切一點" belongs to the workflow that made the artifact,
            # whatever the classifier called it.
            route = session.topic_type
        handler = self._handlers.get(route, self._handlers["general"])
        reply = await handler(
            TurnContext(event=event, route=route, session=session, user_text=text)
        )
        if reply.last_artifact_id is not None:
            # The artifact's workflow owns the follow-ups, even in a session "你好" opened.
            session = session.evolve(
                topic_type=route,
                state_json={**session.state_json, ARTIFACT_KEY: reply.last_artifact_id},
            )
        await self._sessions.save(
            tenant_id=tenant_id, user_id=user_id, state=commit_turn(session, text, reply.text, now)
        )
        return replies.with_truncation_notice(reply.response) if truncated else reply.response

    async def _classify(
        self, event: IncomingEvent, text: str, session: TopicSessionState | None
    ) -> RouteDecision:
        system = ROUTE_SYSTEM
        turns: tuple[Turn, ...] = ()
        if session is not None:
            system = f"{ROUTE_SYSTEM}\n目前話題: {session.topic_type}"
            turns = session.recent_turns[-ROUTE_CONTEXT_TURNS:]
        result = await self._llm.generate(
            LLMRequest(
                tenant_id=event.tenant_id,
                user_id=event.user_id,
                workflow_name=ROUTE_WORKFLOW,
                capability=TaskCapability.TEXT_FAST_ROUTE,
                prompt=PromptParts(system=system, recent_turns=turns, current_user_msg=text),
                max_output_tokens=ROUTE_MAX_OUTPUT,
                json_output=True,
            )
        )
        try:
            return RouteDecision.model_validate_json(result.text)
        except ValidationError:
            return FALLBACK_DECISION

    async def _extract(
        self, event: IncomingEvent, text: str, session: TopicSessionState
    ) -> SessionSlots:
        result = await self._llm.generate(
            LLMRequest(
                tenant_id=event.tenant_id,
                user_id=event.user_id,
                workflow_name=EXTRACT_WORKFLOW,
                capability=TaskCapability.TEXT_FAST_ROUTE,
                prompt=PromptParts(
                    system=EXTRACT_SYSTEM,
                    state_json=session.state_json,
                    recent_turns=session.recent_turns,
                    current_user_msg=text,
                ),
                max_output_tokens=EXTRACT_MAX_OUTPUT,
                json_output=True,
            )
        )
        # Never retried, even when empty: ADR-0003 allows one extraction per session.
        return parse_slots(result.text)
