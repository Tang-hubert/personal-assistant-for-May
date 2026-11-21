"""SQLite implementation of LedgerPort.

record_usage is scoped by tenant_id and user_id (the composite FK guards it). spent_between,
daily_budget, and claim_alert_80 are scoped by tenant_id alone: the daily budget belongs to the
tenant, so its sum and its 80% flag are tenant-level aggregates. That is the third documented
exception to user_id scoping (ARCHITECTURE §5), approved as decision D2.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from src.adapters.storage.orm import LlmUsage, Tenant
from src.domain.ports import Clock, Provider

MICRO_USD = Decimal("0.000001")


class SqlLedger:
    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._clock = clock

    async def record_usage(
        self,
        *,
        tenant_id: str,
        user_id: str,
        provider: Provider,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: Decimal,
        workflow_name: str,
    ) -> None:
        async with self._sessions() as session:
            session.add(
                LlmUsage(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    provider=provider,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost_usd=cost_usd,
                    workflow_name=workflow_name,
                    created_at=self._clock.now(),
                )
            )
            await session.commit()

    async def spent_between(self, tenant_id: str, start: datetime, end: datetime) -> Decimal:
        async with self._sessions() as session:
            total = (
                await session.execute(
                    select(func.coalesce(func.sum(LlmUsage.estimated_cost_usd), 0)).where(
                        LlmUsage.tenant_id == tenant_id,
                        LlmUsage.created_at >= start,
                        LlmUsage.created_at < end,
                    )
                )
            ).scalar_one()
        # SQLite sums NUMERIC as REAL; going through str() and quantizing drops the float noise.
        return Decimal(str(total)).quantize(MICRO_USD)

    async def daily_budget(self, tenant_id: str) -> Decimal:
        async with self._sessions() as session:
            budget = (
                await session.execute(select(Tenant.daily_budget_usd).where(Tenant.id == tenant_id))
            ).scalar_one()
        return Decimal(str(budget))

    async def claim_alert_80(self, tenant_id: str, day: date) -> bool:
        async with self._sessions() as session:
            result = await session.execute(
                update(Tenant)
                .where(
                    Tenant.id == tenant_id,
                    or_(Tenant.budget_alert_80_date.is_(None), Tenant.budget_alert_80_date != day),
                )
                .values(budget_alert_80_date=day)
            )
            await session.commit()
            return result.rowcount == 1  # type: ignore[attr-defined,no-any-return]
