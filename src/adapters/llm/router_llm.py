"""RoutingLLM: the single LLMPort every caller uses (ARCHITECTURE §3).

Per call: budget gate -> provider by capability -> ledger row as soon as a response arrives ->
on LLMUnavailable, a throttled Ops alert, then: a busy model (429/503/504) gets one try on the
same provider's spare model, images and audio included; a text-only request still failing gets
one fallback provider. Every extra try passes the budget gate again and is billed at the model
that answered. A request over the prompt budget cannot reach here: LLMRequest
refuses to be constructed.
"""

from __future__ import annotations

import logging
import time
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

logger = logging.getLogger(__name__)

ALERT_THROTTLE = timedelta(minutes=15)
# Reasons a different model of the same provider may not share; an empty balance or a broken
# reply would hit the spare just the same.
SPARE_REASONS: frozenset[UnavailableReason] = frozenset({"rate_limited", "overloaded"})

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
    "overloaded": ("warning", "{provider} 回應 503/504 模型忙線"),
    "provider_error": ("error", "{provider} 呼叫失敗 (5xx 或連線錯誤)"),
}


class RoutingLLM:
    def __init__(
        self,
        finops: BudgetGuardPort,
        providers: Mapping[Provider, LLMPort],
        notifier: OpsNotifierPort,
        clock: Clock,
        *,
        spares: Mapping[Provider, LLMPort] | None = None,
    ) -> None:
        self._finops = finops
        self._providers = providers
        self._spares = spares or {}
        self._notifier = notifier
        self._clock = clock
        self._last_alert: dict[tuple[Provider, UnavailableReason], datetime] = {}

    async def generate(self, request: LLMRequest) -> LLMResult:
        provider = PRIMARY[request.capability]
        try:
            return await self._call(self._providers[provider], provider, request)
        except LLMUnavailable as err:
            spare = self._spares.get(provider)
            if spare is None or err.reason not in SPARE_REASONS:
                return await self._fall_back(request, err)
        try:
            return await self._call(spare, provider, request)
        except LLMUnavailable as err:
            return await self._fall_back(request, err)

    async def _fall_back(self, request: LLMRequest, err: LLMUnavailable) -> LLMResult:
        fallback = FALLBACK.get(request.capability)
        if fallback is None or request.asset_ids:
            raise err
        return await self._call(self._providers[fallback], fallback, request)

    async def _call(self, port: LLMPort, provider: Provider, request: LLMRequest) -> LLMResult:
        await self._finops.ensure_can_spend(request.tenant_id)
        started = time.monotonic()
        try:
            result = await port.generate(request)
        except LLMUnavailable as err:
            logger.warning(
                "llm failed workflow=%s provider=%s reason=%s detail=%s ms=%d",
                request.workflow_name,
                provider,
                err.reason,
                err.detail,
                (time.monotonic() - started) * 1000,
            )
            if err.billed is not None:
                await self._finops.record(request, err.billed)
            await self._alert(err)
            raise
        logger.info(
            "llm ok workflow=%s provider=%s model=%s ms=%d in=%d out=%d finish=%s",
            request.workflow_name,
            result.provider,
            result.model,
            (time.monotonic() - started) * 1000,
            result.prompt_tokens,
            result.completion_tokens,
            result.finish_reason,
        )
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
