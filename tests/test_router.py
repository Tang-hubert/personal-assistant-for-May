import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from src.adapters.discord_ops.notifier import DiscordWebhookNotifier
from src.adapters.llm.router_llm import RoutingLLM
from src.adapters.storage.engine import create_engine
from src.adapters.storage.ledger_repository import SqlLedger
from src.adapters.storage.media_repository import SqlMediaStore
from src.adapters.storage.session_repository import SqlSessionStore
from src.domain import replies
from src.domain.media import Intent, IntentSelected, encode_callback, resend_prompt
from src.domain.models import BotResponse, IncomingEvent, ReplyTarget
from src.domain.ports import BudgetExhausted, LLMPort, LLMRequest, LLMUnavailable
from src.domain.router import HandlerReply, Router, TurnContext
from src.domain.session import TopicSessionState, TopicType
from src.pipeline.finops_guardian import FinOpsGuardian
from tests.fakes import FakeLLM, FakeNotifier, FrozenClock, Owner
from tests.llm_helpers import Handler, deepseek_on, gemini_on, html_200, truncated_json_200

ROUTE_OK = json.dumps({"route": "general", "topic_shift": False})


@dataclass
class FakeBatches:
    added: list[IncomingEvent] = field(default_factory=list)
    callbacks: list[IncomingEvent] = field(default_factory=list)
    outcome: IntentSelected | BotResponse = field(default_factory=resend_prompt)

    async def add(self, event: IncomingEvent) -> None:
        self.added.append(event)

    async def handle_intent_callback(self, event: IncomingEvent) -> IntentSelected | BotResponse:
        self.callbacks.append(event)
        return self.outcome


@dataclass
class FakeHandler:
    name: str
    contexts: list[TurnContext] = field(default_factory=list)
    error: Exception | None = None
    artifact_id: str | None = None

    async def __call__(self, ctx: TurnContext) -> HandlerReply:
        self.contexts.append(ctx)
        if self.error is not None:
            raise self.error
        return HandlerReply(
            text=f"{self.name}:{ctx.user_text}",
            response=BotResponse(title=self.name),
            last_artifact_id=self.artifact_id,
        )


@dataclass
class FakeTranscriber:
    transcript: str = "翡翠放哪"
    events: list[IncomingEvent] = field(default_factory=list)

    async def __call__(self, event: IncomingEvent) -> str:
        self.events.append(event)
        return self.transcript


@dataclass
class FakeIntent:
    selected: list[IntentSelected] = field(default_factory=list)

    async def __call__(self, event: IncomingEvent, selected: IntentSelected) -> BotResponse:
        self.selected.append(selected)
        return BotResponse(title="workflow")


class Rig:
    def __init__(
        self, engine: AsyncEngine, clock: FrozenClock, owner: Owner, db_path: Path
    ) -> None:
        self.clock = clock
        self.owner = owner
        self.db_path = db_path
        self.notifier = FakeNotifier()
        self.ledger = SqlLedger(engine, clock)
        self.sessions = SqlSessionStore(engine, clock)
        self.route_reply: str | Exception = ROUTE_OK
        self.slots_reply = json.dumps({"topic": "翡翠", "facts": ["綠色"]})
        self.llm = FakeLLM(respond=self._respond)
        self.batches = FakeBatches()
        self.general = FakeHandler("general")
        self.sales = FakeHandler("sales")
        self.transcriber = FakeTranscriber()
        self.intent = FakeIntent()
        self.use_llm(self.llm)
        self._n = 0

    def use_llm(self, llm: LLMPort) -> None:
        self.router = Router(
            finops=FinOpsGuardian(self.ledger, self.notifier, self.clock),
            llm=llm,
            sessions=self.sessions,
            batches=self.batches,
            handlers={"general": self.general, "sales": self.sales},
            transcriber=self.transcriber,
            intent_handler=self.intent,
            clock=self.clock,
        )

    def _respond(self, req: LLMRequest) -> str | Exception:
        return self.route_reply if req.workflow_name == "route" else self.slots_reply

    def calls(self, workflow: str) -> list[LLMRequest]:
        return [req for req in self.llm.calls if req.workflow_name == workflow]

    def event(self, event_type: str = "text", **fields: object) -> IncomingEvent:
        self._n += 1
        base: dict[str, object] = {
            "event_id": f"e{self._n}",
            "tenant_id": self.owner.tenant_id,
            "user_id": self.owner.user_id,
            "channel": "telegram",
            "event_type": event_type,
            "reply_target": ReplyTarget(address="chat-1"),
        }
        return IncomingEvent.model_validate({**base, **fields})

    def text(self, body: str) -> IncomingEvent:
        return self.event(payload_text=body)

    async def spend_whole_budget(self) -> None:
        await self.ledger.record_usage(
            tenant_id=self.owner.tenant_id,
            user_id=self.owner.user_id,
            provider="deepseek",
            model="deepseek-flash",
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=Decimal("0.20"),
            workflow_name="test",
        )

    def session_rows(self) -> list[tuple[str, str]]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT id, status FROM topic_sessions WHERE tenant_id = ? AND user_id = ?"
                " ORDER BY rowid",
                (self.owner.tenant_id, self.owner.user_id),
            ).fetchall()

    async def active(self) -> TopicSessionState:
        state = await self.sessions.get_active(
            tenant_id=self.owner.tenant_id, user_id=self.owner.user_id
        )
        assert state is not None
        return state

    async def has_active(self) -> bool:
        return (
            await self.sessions.get_active(
                tenant_id=self.owner.tenant_id, user_id=self.owner.user_id
            )
            is not None
        )


@pytest.fixture
async def rig(db_path: Path, clock: FrozenClock, owner: Owner) -> AsyncIterator[Rig]:
    engine = create_engine(db_path)
    yield Rig(engine, clock, owner, db_path)
    await engine.dispose()


async def test_dod_b_exhausted_budget_answers_rest_with_zero_llm_calls(rig: Rig) -> None:
    await rig.spend_whole_budget()
    events = [
        rig.text("翡翠放哪"),
        rig.event("audio", asset_ids=("v1",), audio_duration_s=10.0),
        rig.event("action_callback", callback_data=encode_callback("b1", Intent.SALES)),
        rig.event("action_callback", callback_data=replies.FINISH_CALLBACK),
    ]
    for event in events:
        assert await rig.router.handle(event) == replies.rest_message()

    assert rig.llm.calls == []
    assert rig.general.contexts == rig.transcriber.events == rig.batches.callbacks == []
    assert rig.notifier.levels() == ["critical"]


async def test_d1_photos_still_reach_the_aggregator_at_full_budget(rig: Rig) -> None:
    await rig.spend_whole_budget()
    photo = rig.event("image", asset_ids=("p1",))

    assert await rig.router.handle(photo) is None
    assert rig.batches.added == [photo]
    assert rig.llm.calls == []


async def test_dod_g_batch_callback_routes_with_zero_llm_calls(rig: Rig) -> None:
    rig.batches.outcome = IntentSelected(
        batch_id="b1", intent=Intent.LECTURE, kept_asset_ids=("p1",)
    )
    tap = rig.event("action_callback", callback_data=encode_callback("b1", Intent.LECTURE))

    assert await rig.router.handle(tap) == BotResponse(title="workflow")
    assert rig.intent.selected == [rig.batches.outcome]

    rig.batches.outcome = resend_prompt()
    assert await rig.router.handle(tap) == resend_prompt()
    assert rig.llm.calls == []


async def test_dod_g_finish_callback_closes_the_session_with_zero_llm_calls(rig: Rig) -> None:
    await rig.router.handle(rig.text("翡翠放哪"))
    llm_calls = len(rig.llm.calls)

    reply = await rig.router.handle(
        rig.event("action_callback", callback_data=replies.FINISH_CALLBACK)
    )

    assert reply == replies.topic_finished()
    assert not await rig.has_active()
    assert len(rig.llm.calls) == llm_calls
    assert len(replies.finish_button().label) <= 20


async def test_dod_j_121_second_voice_is_rejected_before_any_llm_call(rig: Rig) -> None:
    reply = await rig.router.handle(rig.event("audio", asset_ids=("v1",), audio_duration_s=121.0))

    assert reply == replies.voice_too_long()
    assert rig.llm.calls == []
    assert rig.transcriber.events == []


async def test_120_second_voice_is_transcribed_and_routed_like_text(rig: Rig) -> None:
    voice = rig.event("audio", asset_ids=("v1",), audio_duration_s=120.0)

    assert await rig.router.handle(voice) == BotResponse(title="general")
    assert rig.transcriber.events == [voice]
    assert rig.general.contexts[0].user_text == "翡翠放哪"
    assert rig.calls("route")[0].prompt.current_user_msg == "翡翠放哪"


async def test_unsupported_event_gets_the_fixed_reply(rig: Rig) -> None:
    assert await rig.router.handle(rig.event("unsupported")) == replies.unsupported_message()
    assert rig.llm.calls == []


async def test_dod_c_fourth_message_extracts_once_and_seventh_force_closes(rig: Rig) -> None:
    for n in (1, 2, 3):
        await rig.router.handle(rig.text(f"U{n}"))
    assert rig.calls("slot_extract") == []

    await rig.router.handle(rig.text("U4"))
    assert len(rig.calls("slot_extract")) == 1
    extraction = rig.calls("slot_extract")[0]
    assert [t.text for t in extraction.prompt.recent_turns] == [
        "U1", "general:U1", "U2", "general:U2", "U3", "general:U3",
    ]  # fmt: skip
    state = await rig.active()
    assert [t.text for t in state.recent_turns] == ["U4", "general:U4"]
    assert json.loads(state.state_json["slots"])["topic"] == "翡翠"
    assert rig.general.contexts[-1].session.recent_turns == ()

    for n in (5, 6):
        await rig.router.handle(rig.text(f"U{n}"))
        state = await rig.active()
        assert len(state.recent_turns) <= 6

    handled = len(rig.general.contexts)
    assert await rig.router.handle(rig.text("U7")) == replies.new_topic_prompt()
    assert len(rig.calls("slot_extract")) == 1
    assert len(rig.general.contexts) == handled
    assert not await rig.has_active()

    await rig.router.handle(rig.text("U8"))
    assert rig.general.contexts[-1].session.recent_turns == ()
    assert rig.general.contexts[-1].session.extraction_count == 0


async def test_invalid_slot_json_stores_empty_slots_without_retry(rig: Rig) -> None:
    rig.slots_reply = "not json"
    for n in range(4):
        await rig.router.handle(rig.text(f"U{n}"))

    assert len(rig.calls("slot_extract")) == 1
    state = await rig.active()
    assert state.extraction_count == 1
    assert json.loads(state.state_json["slots"]) == {
        "topic": "",
        "facts": [],
        "pending_request": None,
    }


async def _slots_after_extraction(rig: Rig) -> dict[str, object]:
    for n in range(4):
        await rig.router.handle(rig.text(f"U{n}"))
    assert len(rig.calls("slot_extract")) == 1
    state = await rig.active()
    assert state.extraction_count == 1
    slots: dict[str, object] = json.loads(state.state_json["slots"])
    return slots


async def test_a_sixth_fact_keeps_the_first_five_and_the_topic(rig: Rig) -> None:
    facts = [f"重點{n}" for n in range(1, 7)]
    rig.slots_reply = json.dumps({"topic": "翡翠", "facts": facts, "pending_request": None})

    slots = await _slots_after_extraction(rig)

    assert slots == {"topic": "翡翠", "facts": facts[:5], "pending_request": None}


async def test_an_unknown_slot_key_is_ignored_and_the_topic_kept(rig: Rig) -> None:
    rig.slots_reply = json.dumps(
        {"topic": "翡翠", "facts": ["綠色"], "pending_request": "找證書", "mood": "開心"}
    )

    slots = await _slots_after_extraction(rig)

    assert slots == {"topic": "翡翠", "facts": ["綠色"], "pending_request": "找證書"}


async def test_dod_d_topic_shift_opens_a_zero_history_session(rig: Rig) -> None:
    await rig.router.handle(rig.text("翡翠放哪"))
    first = await rig.active()

    rig.route_reply = json.dumps({"route": "sales", "topic_shift": True})
    assert await rig.router.handle(rig.text("幫我寫珍珠文案")) == BotResponse(title="sales")

    ctx = rig.sales.contexts[-1]
    assert ctx.session.recent_turns == ()
    assert ctx.session.topic_type == "sales"
    assert ctx.session.id != first.id
    assert [status for _, status in rig.session_rows()] == ["closed", "active"]


async def test_dod_d_idle_15_minutes_closes_lazily_and_starts_clean(rig: Rig) -> None:
    await rig.router.handle(rig.text("翡翠放哪"))
    rig.clock.instant += timedelta(minutes=14, seconds=59)
    await rig.router.handle(rig.text("還有呢"))
    assert len(rig.general.contexts[-1].session.recent_turns) == 2

    rig.clock.instant += timedelta(minutes=15)
    await rig.router.handle(rig.text("新的問題"))

    assert rig.general.contexts[-1].session.recent_turns == ()
    assert [status for _, status in rig.session_rows()] == ["closed", "active"]


async def test_dod_h_concurrent_events_for_one_user_keep_one_active_session(rig: Rig) -> None:
    await asyncio.gather(rig.router.handle(rig.text("一")), rig.router.handle(rig.text("二")))

    assert [status for _, status in rig.session_rows()] == ["active"]
    state = await rig.active()
    assert len(state.recent_turns) == 4


async def test_provider_outage_becomes_the_gentle_busy_reply(rig: Rig) -> None:
    rig.route_reply = LLMUnavailable("balance_exhausted", "deepseek")

    reply = await rig.router.handle(rig.text("翡翠放哪"))

    assert reply == replies.busy_message()
    assert "deepseek" not in reply.model_dump_json()


async def test_budget_running_out_mid_turn_becomes_the_rest_reply(rig: Rig) -> None:
    rig.general.error = BudgetExhausted("t")
    assert await rig.router.handle(rig.text("翡翠放哪")) == replies.rest_message()


async def test_851_chars_reach_the_llm_as_850_and_the_reply_says_so(rig: Rig) -> None:
    reply = await rig.router.handle(rig.text("字" * 851))

    assert rig.calls("route")[0].prompt.current_user_msg == "字" * 850
    assert rig.general.contexts[0].user_text == "字" * 850
    assert reply is not None
    assert replies.TRUNCATED_NOTICE in reply.bullets


async def test_classification_is_a_json_fast_route_call(rig: Rig) -> None:
    await rig.router.handle(rig.text("翡翠放哪"))
    (call,) = rig.calls("route")
    assert call.capability == "text_fast_route"
    assert call.json_output is True
    assert call.prompt.current_user_msg == "翡翠放哪"


@pytest.mark.parametrize("bad", ["not json", '{"route": "weather"}', "[]"])
async def test_broken_classification_falls_back_to_general(rig: Rig, bad: str) -> None:
    rig.route_reply = bad
    assert await rig.router.handle(rig.text("翡翠放哪")) == BotResponse(title="general")


async def test_route_without_a_handler_uses_general(rig: Rig) -> None:
    route: TopicType = "memory"
    rig.route_reply = json.dumps({"route": route, "topic_shift": False})
    assert await rig.router.handle(rig.text("翡翠放哪")) == BotResponse(title="general")
    assert rig.general.contexts[-1].route == "memory"


def _gemini_route_ok(req: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": ROUTE_OK}]}}]}
    )


def _real_providers(rig: Rig, media: SqlMediaStore, deepseek: Handler, gemini: Handler) -> None:
    rig.use_llm(
        RoutingLLM(
            FinOpsGuardian(rig.ledger, rig.notifier, rig.clock),
            {"deepseek": deepseek_on(deepseek), "gemini": gemini_on(gemini, media)},
            rig.notifier,
            rig.clock,
        )
    )


async def test_deepseek_html_200_falls_back_to_gemini_and_alerts_once(
    rig: Rig, media_store: SqlMediaStore
) -> None:
    _real_providers(rig, media_store, html_200, _gemini_route_ok)

    assert await rig.router.handle(rig.text("翡翠放哪")) == BotResponse(title="general")
    assert await rig.router.handle(rig.text("還有呢")) == BotResponse(title="general")
    assert rig.notifier.levels() == ["error"]


async def test_garbage_from_both_providers_becomes_the_busy_reply(
    rig: Rig, media_store: SqlMediaStore
) -> None:
    _real_providers(rig, media_store, html_200, truncated_json_200)

    assert await rig.router.handle(rig.text("翡翠放哪")) == replies.busy_message()
    assert rig.notifier.levels() == ["error", "error"]
    assert rig.general.contexts == []


async def test_a_broken_ops_webhook_at_80_percent_still_answers(rig: Rig) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(204)))
    rig.notifier = DiscordWebhookNotifier(SecretStr("https://discord.invalid/hook\r"), client)
    rig.use_llm(rig.llm)
    await rig.ledger.record_usage(
        tenant_id=rig.owner.tenant_id,
        user_id=rig.owner.user_id,
        provider="deepseek",
        model="deepseek-flash",
        prompt_tokens=0,
        completion_tokens=0,
        cost_usd=Decimal("0.17"),
        workflow_name="test",
    )

    assert await rig.router.handle(rig.text("翡翠放哪")) == BotResponse(title="general")


async def test_handler_artifact_id_is_committed_into_state_json(rig: Rig) -> None:
    who = {"tenant_id": rig.owner.tenant_id, "user_id": rig.owner.user_id}
    await rig.sessions.open_fresh(
        **who, topic_type="sales", state_json={"last_artifact_id": "old", "workflow": "sales_copy"}
    )
    rig.sales.artifact_id = "new"

    await rig.router.handle(rig.text("改親切一點"))  # classifier says general: sticks to sales

    assert len(rig.sales.contexts) == 1 and rig.general.contexts == []
    after = await rig.sessions.get_active(**who)
    assert after is not None
    assert after.state_json == {"last_artifact_id": "new", "workflow": "sales_copy"}
    assert len(after.recent_turns) == 2


async def test_the_route_is_logged_without_the_users_words(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="src.domain.router")
    rig.route_reply = json.dumps({"route": "sales", "topic_shift": False})

    await rig.router.handle(rig.text("幫我寫珍珠文案"))

    assert "route=sales topic_shift=False session=new" in caplog.text
    assert "珍珠" not in caplog.text


async def test_a_busy_reply_says_why_in_the_log(
    rig: Rig, media_store: SqlMediaStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="src.domain.router")
    _real_providers(rig, media_store, html_200, truncated_json_200)

    assert await rig.router.handle(rig.text("翡翠放哪")) == replies.busy_message()
    assert "busy reply event=text provider=gemini reason=provider_error" in caplog.text


async def test_the_rest_message_is_logged(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="src.domain.router")
    await rig.spend_whole_budget()

    assert await rig.router.handle(rig.text("翡翠放哪")) == replies.rest_message()
    assert "budget exhausted" in caplog.text
