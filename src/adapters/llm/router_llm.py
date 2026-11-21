"""RoutingLLM: the single LLMPort every caller uses (ARCHITECTURE §3).

Per call: budget gate -> provider by capability -> ledger row as soon as a response arrives ->
on LLMUnavailable, a throttled Ops alert and, for text-only requests, one fallback provider that
passes the budget gate again. A request over the prompt budget cannot reach here: LLMRequest
refuses to be constructed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Literal

from src.domain.models import TaskCapability
from src.domain.ports import (
    BudgetGuardPort,
    Clock,
    LLMPort,
    LLMRequest,
    LLMResult,
    LLMUnavailable,
    OpsNotifierPort,
    Provider,
    UnavailableReason,
)

ALERT_THROTTLE = timedelta(minutes=15)

PRIMARY: dict[TaskCapability, Provider] = {
    TaskCapability.MULTIMODAL_IMAGE: "gemini",
    TaskCapability.MULTIMODAL_AUDIO: "gemini",
    TaskCapability.COPYWRITING_TW: "gemini",
    TaskCapability.TEXT_FAST_ROUTE: "deepseek",
}
# Multimodal capabilities have no fallback: DeepSeek cannot see images or hear audio.
FALLBACK: dict[TaskCapability, Provider] = {
    TaskCapability.COPYWRITING_TW: "deepseek",
    TaskCapability.TEXT_FAST_ROUTE: "gemini",
}

_ALERTS: dict[UnavailableReason, tuple[Literal["warning", "critical", "error"], str]] = {
    "balance_exhausted": ("critical", "{provider} 預付餘額用完 請儲值"),
    "rate_limited": ("warning", "{provider} 回應 429 流量限制"),
    "provider_error": ("error", "{provider} 呼叫失敗 (5xx 或連線錯誤)"),
}


class RoutingLLM:
    def __init__(
        self,
        finops: BudgetGuardPort,
        providers: Mapping[Provider, LLMPort],
        notifier: OpsNotifierPort,
        clock: Clock,
    ) -> None:
        self._finops = finops
        self._providers = providers
        self._notifier = notifier
        self._clock = clock
        self._last_alert: dict[tuple[Provider, UnavailableReason], datetime] = {}

    async def generate(self, request: LLMRequest) -> LLMResult:
        try:
            return await self._call(PRIMARY[request.capability], request)
        except LLMUnavailable:
            fallback = FALLBACK.get(request.capability)
            if fallback is None or request.asset_ids:
                raise
        return await self._call(fallback, request)

    async def _call(self, provider: Provider, request: LLMRequest) -> LLMResult:
        await self._finops.ensure_can_spend(request.tenant_id)
        try:
            result = await self._providers[provider].generate(request)
        except LLMUnavailable as err:
            if err.billed is not None:
                await self._finops.record(request, err.billed)
            await self._alert(err)
            raise
        # Recorded before any caller parses the text, so a bad reply is still billed.
        await self._finops.record(request, result)
        return result

    async def _alert(self, err: LLMUnavailable) -> None:
        key = (err.provider, err.reason)
        now = self._clock.now()
        last = self._last_alert.get(key)
        if last is not None and now - last < ALERT_THROTTLE:
            return
        self._last_alert[key] = now
        level, template = _ALERTS[err.reason]
        await self._notifier.notify(level, template.format(provider=err.provider))
