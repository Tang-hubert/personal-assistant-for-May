"""FinOps Guardian (ARCHITECTURE §6): Taipei-day spend against the tenant's daily budget.

The check runs before a call and the cost is recorded after it, so concurrent users of one
tenant can overshoot the budget by at most the calls already in flight. That is accepted for a
single-process, family-sized deployment.

The 80% alert is claimed in the database before it is sent, so it goes out at most once per
Taipei day even across restarts; a failed send is not retried. The 100% alert is deduplicated
in memory per (tenant, Taipei day), so a restart can repeat it once.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_UP, Decimal

from src.config import price_for
from src.domain.ports import (
    BudgetExhausted,
    BudgetStatus,
    Clock,
    LedgerPort,
    LLMRequest,
    LLMResult,
    OpsNotifierPort,
)
from src.domain.timeutil import taipei_date, taipei_day_utc_range

WARN_RATIO = Decimal("0.8")
MICRO_USD = Decimal("0.000001")
PER_TOKENS = Decimal(1_000_000)


class FinOpsGuardian:
    def __init__(self, ledger: LedgerPort, notifier: OpsNotifierPort, clock: Clock) -> None:
        self._ledger = ledger
        self._notifier = notifier
        self._clock = clock
        self._critical_sent: set[tuple[str, date]] = set()

    async def status(self, tenant_id: str) -> BudgetStatus:
        now = self._clock.now()
        today = taipei_date(now)
        start, end = taipei_day_utc_range(now)
        spent = await self._ledger.spent_between(tenant_id, start, end)
        budget = await self._ledger.daily_budget(tenant_id)
        if spent >= budget:
            if (tenant_id, today) not in self._critical_sent:
                self._critical_sent.add((tenant_id, today))
                await self._notifier.notify(
                    "critical",
                    f"租戶 {tenant_id} 今日 LLM 預算用完 ({spent} / {budget} USD) 已暫停 AI 呼叫",
                )
            return BudgetStatus.EXHAUSTED
        if spent >= budget * WARN_RATIO:
            if await self._ledger.claim_alert_80(tenant_id, today):
                await self._notifier.notify(
                    "warning", f"租戶 {tenant_id} 今日 LLM 花費已達 80% ({spent} / {budget} USD)"
                )
            return BudgetStatus.WARN
        return BudgetStatus.OK

    async def ensure_can_spend(self, tenant_id: str) -> None:
        if await self.status(tenant_id) is BudgetStatus.EXHAUSTED:
            raise BudgetExhausted(tenant_id)

    async def record(self, request: LLMRequest, result: LLMResult) -> None:
        tier = price_for(result.model, taipei_date(self._clock.now()))
        cost = (
            result.prompt_tokens * tier.input_per_1m + result.completion_tokens * tier.output_per_1m
        ) / PER_TOKENS
        await self._ledger.record_usage(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            provider=result.provider,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            # Rounded up: FinOps would rather over-count a fraction of a micro-dollar.
            cost_usd=cost.quantize(MICRO_USD, rounding=ROUND_UP),
            workflow_name=request.workflow_name,
        )
