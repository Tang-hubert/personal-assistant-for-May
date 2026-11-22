import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from src.adapters.llm.router_llm import RoutingLLM
from src.adapters.storage.engine import create_engine
from src.adapters.storage.ledger_repository import SqlLedger
from src.domain.models import TaskCapability
from src.domain.ports import BudgetExhausted, LLMRequest, LLMResult, LLMUnavailable
from src.pipeline.finops_guardian import FinOpsGuardian
from tests.fakes import FakeLLM, FakeNotifier, FrozenClock, Owner
from tests.llm_helpers import deepseek_on, request


class Rig:
    def __init__(self, ledger: SqlLedger, clock: FrozenClock, owner: Owner, db_path: Path) -> None:
        self.ledger = ledger
        self.clock = clock
        self.owner = owner
        self.db_path = db_path
        self.notifier = FakeNotifier()
        self.gemini = FakeLLM(provider="gemini", model="gemini-3.8-flash")
        self.deepseek = FakeLLM(provider="deepseek", model="deepseek-flash")
        self.spare: FakeLLM | None = None
        self.build()

    def build(self) -> None:
        self.llm = RoutingLLM(
            FinOpsGuardian(self.ledger, self.notifier, self.clock),
            {"gemini": self.gemini, "deepseek": self.deepseek},
            self.notifier,
            self.clock,
            spares={"gemini": self.spare} if self.spare is not None else {},
        )

    def with_spare(self) -> FakeLLM:
        self.spare = FakeLLM(provider="gemini", model="gemini-3.5-flash-lite")
        self.build()
        return self.spare

    def request(self, capability: TaskCapability, **overrides: object) -> LLMRequest:
        return request(
            capability, tenant_id=self.owner.tenant_id, user_id=self.owner.user_id, **overrides
        )

    def ledger_rows(self) -> list[tuple[str, str, int, int, float]]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT provider, model, prompt_tokens, completion_tokens, estimated_cost_usd"
                " FROM llm_usage_ledger ORDER BY rowid"
            ).fetchall()

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


@pytest.fixture
async def rig(db_path: Path, clock: FrozenClock, owner: Owner) -> AsyncIterator[Rig]:
    engine = create_engine(db_path)
    yield Rig(SqlLedger(engine, clock), clock, owner, db_path)
    await engine.dispose()


@pytest.mark.parametrize(
    ("capability", "extra", "provider"),
    [
        (TaskCapability.MULTIMODAL_IMAGE, {"asset_ids": ("i1", "i2")}, "gemini"),
        (
            TaskCapability.MULTIMODAL_AUDIO,
            {"asset_ids": ("v1",), "audio_duration_s": 30.0},
            "gemini",
        ),
        (TaskCapability.COPYWRITING_TW, {}, "gemini"),
        (TaskCapability.TEXT_FAST_ROUTE, {}, "deepseek"),
    ],
)
async def test_dod_a_capability_picks_the_provider(
    rig: Rig, capability: TaskCapability, extra: dict[str, object], provider: str
) -> None:
    result = await rig.llm.generate(rig.request(capability, **extra))

    assert result.provider == provider
    chosen, other = (
        (rig.gemini, rig.deepseek) if provider == "gemini" else (rig.deepseek, rig.gemini)
    )
    assert len(chosen.calls) == 1
    assert other.calls == []


async def test_every_successful_call_writes_one_priced_ledger_row(rig: Rig) -> None:
    await rig.llm.generate(rig.request(TaskCapability.TEXT_FAST_ROUTE))
    await rig.llm.generate(rig.request(TaskCapability.COPYWRITING_TW))

    # FakeLLM reports 1,000 prompt + 100 completion tokens.
    assert rig.ledger_rows() == [
        ("deepseek", "deepseek-flash", 1000, 100, pytest.approx(0.00042)),
        ("gemini", "gemini-3.8-flash", 1000, 100, pytest.approx(0.001125)),
    ]


async def test_402_alerts_critical_once_per_15_minutes_and_does_not_fall_back(rig: Rig) -> None:
    rig.gemini.respond = lambda req: LLMUnavailable("balance_exhausted", "gemini")
    images = {"asset_ids": ("i1",)}

    for _ in range(2):
        with pytest.raises(LLMUnavailable):
            await rig.llm.generate(rig.request(TaskCapability.MULTIMODAL_IMAGE, **images))
    assert rig.notifier.levels() == ["critical"]
    assert "gemini" in rig.notifier.sent[0][1]
    assert rig.deepseek.calls == []
    assert rig.ledger_rows() == []

    rig.clock.instant += timedelta(minutes=15)
    with pytest.raises(LLMUnavailable):
        await rig.llm.generate(rig.request(TaskCapability.MULTIMODAL_IMAGE, **images))
    assert rig.notifier.levels() == ["critical", "critical"]


async def test_fast_route_429_falls_back_to_gemini_and_records_it(rig: Rig) -> None:
    rig.deepseek.respond = lambda req: LLMUnavailable("rate_limited", "deepseek")

    result = await rig.llm.generate(rig.request(TaskCapability.TEXT_FAST_ROUTE, json_output=True))

    assert result.provider == "gemini"
    assert rig.gemini.calls[0].json_output is True
    assert rig.notifier.levels() == ["warning"]
    assert [row[0] for row in rig.ledger_rows()] == ["gemini"]


async def test_text_copywriting_falls_back_to_deepseek(rig: Rig) -> None:
    rig.gemini.respond = lambda req: LLMUnavailable("provider_error", "gemini")

    result = await rig.llm.generate(rig.request(TaskCapability.COPYWRITING_TW))

    assert result.provider == "deepseek"
    assert rig.notifier.levels() == ["error"]


async def test_failed_fallback_alerts_both_providers_and_raises(rig: Rig) -> None:
    rig.deepseek.respond = lambda req: LLMUnavailable("balance_exhausted", "deepseek")
    rig.gemini.respond = lambda req: LLMUnavailable("rate_limited", "gemini")

    with pytest.raises(LLMUnavailable):
        await rig.llm.generate(rig.request(TaskCapability.TEXT_FAST_ROUTE))
    assert rig.notifier.levels() == ["critical", "warning"]


async def test_exhausted_budget_raises_before_any_provider_call(rig: Rig) -> None:
    await rig.spend_whole_budget()

    with pytest.raises(BudgetExhausted):
        await rig.llm.generate(rig.request(TaskCapability.TEXT_FAST_ROUTE))
    assert rig.gemini.calls == rig.deepseek.calls == []
    assert rig.notifier.levels() == ["critical"]


async def test_fallback_is_gated_by_the_budget_again(rig: Rig) -> None:
    class ExhaustingDeepSeek(FakeLLM):
        async def generate(self, request: LLMRequest) -> LLMResult:
            self.calls.append(request)
            await rig.spend_whole_budget()
            raise LLMUnavailable("rate_limited", "deepseek")

    rig.deepseek = ExhaustingDeepSeek()
    rig.build()

    with pytest.raises(BudgetExhausted):
        await rig.llm.generate(rig.request(TaskCapability.TEXT_FAST_ROUTE))
    assert len(rig.deepseek.calls) == 1
    assert rig.gemini.calls == []


def test_seven_images_cannot_even_form_a_request(rig: Rig) -> None:
    with pytest.raises(ValidationError):
        rig.request(TaskCapability.MULTIMODAL_IMAGE, asset_ids=tuple(f"i{n}" for n in range(7)))
    assert rig.gemini.calls == rig.deepseek.calls == []


async def test_billed_malformed_reply_is_recorded_then_alerted_and_falls_back(rig: Rig) -> None:
    def no_choices(req: httpx.Request) -> httpx.Response:
        usage = {"prompt_tokens": 40, "completion_tokens": 7, "total_tokens": 47}
        return httpx.Response(
            200,
            json={"id": "c", "object": "chat.completion", "created": 0, "model": "m",
                  "choices": [], "usage": usage},
        )  # fmt: skip

    rig.deepseek = deepseek_on(no_choices)
    rig.build()

    result = await rig.llm.generate(rig.request(TaskCapability.TEXT_FAST_ROUTE))

    assert result.provider == "gemini"
    assert rig.notifier.levels() == ["error"]
    assert rig.ledger_rows() == [
        ("deepseek", "deepseek-flash", 40, 7, pytest.approx(0.000021)),
        ("gemini", "gemini-3.8-flash", 1000, 100, pytest.approx(0.001125)),
    ]


@pytest.mark.parametrize("reason", ["overloaded", "rate_limited"])
async def test_a_busy_gemini_model_hands_an_image_request_to_its_spare_model(
    rig: Rig, reason: str
) -> None:
    spare = rig.with_spare()
    rig.gemini.respond = lambda req: LLMUnavailable(reason, "gemini", detail="http 503")  # type: ignore[arg-type]

    result = await rig.llm.generate(
        rig.request(TaskCapability.MULTIMODAL_IMAGE, asset_ids=("i1", "i2"))
    )

    assert result.model == "gemini-3.5-flash-lite"
    assert spare.calls[0].asset_ids == ("i1", "i2")
    assert rig.deepseek.calls == []
    assert [row[:2] for row in rig.ledger_rows()] == [("gemini", "gemini-3.5-flash-lite")]


async def test_a_provider_error_does_not_try_the_spare(rig: Rig) -> None:
    spare = rig.with_spare()
    rig.gemini.respond = lambda req: LLMUnavailable("provider_error", "gemini")

    with pytest.raises(LLMUnavailable):
        await rig.llm.generate(rig.request(TaskCapability.MULTIMODAL_IMAGE, asset_ids=("i1",)))
    assert spare.calls == []


async def test_a_failed_spare_still_lets_text_fall_back_to_deepseek(rig: Rig) -> None:
    spare = rig.with_spare()
    rig.gemini.respond = lambda req: LLMUnavailable("overloaded", "gemini")
    spare.respond = lambda req: LLMUnavailable("overloaded", "gemini")

    result = await rig.llm.generate(rig.request(TaskCapability.COPYWRITING_TW))

    assert result.provider == "deepseek"
    assert len(spare.calls) == 1


async def test_the_spare_is_gated_by_the_budget_again(rig: Rig) -> None:
    spare = rig.with_spare()

    class ExhaustingGemini(FakeLLM):
        async def generate(self, request: LLMRequest) -> LLMResult:
            self.calls.append(request)
            await rig.spend_whole_budget()
            raise LLMUnavailable("overloaded", "gemini")

    rig.gemini = ExhaustingGemini(provider="gemini", model="gemini-3.8-flash")
    rig.build()

    with pytest.raises(BudgetExhausted):
        await rig.llm.generate(rig.request(TaskCapability.MULTIMODAL_IMAGE, asset_ids=("i1",)))
    assert spare.calls == []


async def test_each_call_logs_workflow_model_and_tokens_without_the_prompt(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.gemini.respond = lambda req: LLMUnavailable("overloaded", "gemini", detail="http 503")

    with caplog.at_level("INFO", logger="src.adapters.llm.router_llm"):
        await rig.llm.generate(
            rig.request(TaskCapability.COPYWRITING_TW, workflow_name="ios_helper")
        )

    assert "llm failed workflow=ios_helper provider=gemini reason=overloaded detail=http 503" in (
        caplog.text
    )
    assert "llm ok workflow=ios_helper provider=deepseek model=deepseek-flash" in caplog.text
    assert "in=1000 out=100" in caplog.text
    assert "幫我改親切一點" not in caplog.text
