import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from src.adapters.storage.engine import create_engine
from src.adapters.storage.ledger_repository import SqlLedger
from src.adapters.storage.orm import Tenant
from src.adapters.storage.repository import SqlStorage
from src.domain.models import TaskCapability
from src.domain.ports import BudgetExhausted, BudgetStatus, LLMResult
from src.pipeline.finops_guardian import FinOpsGuardian
from tests.fakes import FakeNotifier, FrozenClock, Owner
from tests.llm_helpers import request

# 2026-11-21 11:00 Taipei.
NOON_ISH = datetime(2026, 11, 21, 3, 0, tzinfo=UTC)


class Rig:
    def __init__(self, db_path: Path, clock: FrozenClock, owner: Owner) -> None:
        self.db_path = db_path
        self.clock = clock
        self.owner = owner
        self.notifier = FakeNotifier()
        self._engines: list[AsyncEngine] = []
        self.restart()

    def restart(self) -> None:
        engine = create_engine(self.db_path)
        self._engines.append(engine)
        self.ledger = SqlLedger(engine, self.clock)
        self.guardian = FinOpsGuardian(self.ledger, self.notifier, self.clock)

    async def spend(self, usd: str, owner: Owner | None = None) -> None:
        who = owner or self.owner
        await self.ledger.record_usage(
            tenant_id=who.tenant_id,
            user_id=who.user_id,
            provider="deepseek",
            model="deepseek-flash",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=Decimal(usd),
            workflow_name="test",
        )

    async def close(self) -> None:
        for engine in self._engines:
            await engine.dispose()


@pytest.fixture
async def rig(db_path: Path, clock: FrozenClock, owner: Owner) -> AsyncIterator[Rig]:
    clock.instant = NOON_ISH
    built = Rig(db_path, clock, owner)
    yield built
    await built.close()


async def test_taipei_day_boundary_is_16_00_utc(rig: Rig) -> None:
    rig.clock.instant = datetime(2026, 11, 21, 15, 59, 59, tzinfo=UTC)
    await rig.spend("0.20")
    assert await rig.guardian.status(rig.owner.tenant_id) is BudgetStatus.EXHAUSTED

    rig.clock.instant = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)
    assert await rig.guardian.status(rig.owner.tenant_id) is BudgetStatus.OK


async def test_just_under_80_percent_sends_nothing(rig: Rig) -> None:
    await rig.spend("0.159")
    assert await rig.guardian.status(rig.owner.tenant_id) is BudgetStatus.OK
    assert rig.notifier.sent == []


async def test_80_percent_warns_once_per_taipei_day_across_a_restart(rig: Rig) -> None:
    await rig.spend("0.16")
    assert await rig.guardian.status(rig.owner.tenant_id) is BudgetStatus.WARN
    assert await rig.guardian.status(rig.owner.tenant_id) is BudgetStatus.WARN
    assert rig.notifier.levels() == ["warning"]

    rig.restart()
    assert await rig.guardian.status(rig.owner.tenant_id) is BudgetStatus.WARN
    assert rig.notifier.levels() == ["warning"]

    rig.clock.instant = NOON_ISH + timedelta(days=1)
    await rig.spend("0.17")
    assert await rig.guardian.status(rig.owner.tenant_id) is BudgetStatus.WARN
    assert rig.notifier.levels() == ["warning", "warning"]


async def test_100_percent_blocks_and_alerts_critical_once_per_day(rig: Rig) -> None:
    await rig.spend("0.20")
    assert await rig.guardian.status(rig.owner.tenant_id) is BudgetStatus.EXHAUSTED
    with pytest.raises(BudgetExhausted):
        await rig.guardian.ensure_can_spend(rig.owner.tenant_id)
    assert rig.notifier.levels() == ["critical"]

    rig.clock.instant = NOON_ISH + timedelta(days=1)
    await rig.spend("0.25")
    assert await rig.guardian.status(rig.owner.tenant_id) is BudgetStatus.EXHAUSTED
    assert rig.notifier.levels() == ["critical", "critical"]


async def test_other_tenants_spend_does_not_count(rig: Rig, other_owner: Owner) -> None:
    await rig.spend("5.00", owner=other_owner)
    assert await rig.guardian.status(rig.owner.tenant_id) is BudgetStatus.OK
    await rig.guardian.ensure_can_spend(rig.owner.tenant_id)


async def test_sum_keeps_micro_dollar_precision(rig: Rig) -> None:
    for _ in range(1000):
        await rig.spend("0.000123")
    start = NOON_ISH - timedelta(hours=1)
    assert await rig.ledger.spent_between(
        rig.owner.tenant_id, start, start + timedelta(hours=2)
    ) == Decimal("0.123000")


async def test_budget_is_read_from_the_tenant_row(rig: Rig, storage: SqlStorage) -> None:
    async with storage.session() as session:
        await session.execute(
            update(Tenant).where(Tenant.id == rig.owner.tenant_id).values(daily_budget_usd=1)
        )
        await session.commit()
    await rig.spend("0.50")
    assert await rig.guardian.status(rig.owner.tenant_id) is BudgetStatus.OK


def _result(model: str, prompt: int, completion: int) -> LLMResult:
    provider = "gemini" if model.startswith("gemini") else "deepseek"
    return LLMResult(
        text="x",
        provider=provider,
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
    )


async def test_record_writes_cost_from_the_price_table(rig: Rig) -> None:
    req = request(
        TaskCapability.COPYWRITING_TW,
        tenant_id=rig.owner.tenant_id,
        user_id=rig.owner.user_id,
        workflow_name="sales_copy",
    )
    await rig.guardian.record(req, _result("gemini-3.8-flash", 1_000_000, 100_000))
    start, end = NOON_ISH - timedelta(hours=1), NOON_ISH + timedelta(hours=1)
    # 0.75 + 0.1 * 3.75
    assert await rig.ledger.spent_between(rig.owner.tenant_id, start, end) == Decimal("1.125000")
    with closing(sqlite3.connect(rig.db_path)) as conn:
        rows = conn.execute(
            "SELECT tenant_id, user_id, provider, model, prompt_tokens, completion_tokens,"
            " workflow_name FROM llm_usage_ledger"
        ).fetchall()
    assert rows == [
        (
            rig.owner.tenant_id,
            rig.owner.user_id,
            "gemini",
            "gemini-3.8-flash",
            1_000_000,
            100_000,
            "sales_copy",
        )
    ]


async def test_gemini_price_switches_on_taipei_2027_01_01(rig: Rig) -> None:
    req = request(tenant_id=rig.owner.tenant_id, user_id=rig.owner.user_id)
    rig.clock.instant = datetime(2026, 12, 31, 16, 0, tzinfo=UTC)  # 2027-01-01 00:00 Taipei
    await rig.guardian.record(req, _result("gemini-3.8-flash", 1_000_000, 0))
    start = rig.clock.instant
    assert await rig.ledger.spent_between(
        rig.owner.tenant_id, start, start + timedelta(hours=1)
    ) == Decimal("1.500000")
