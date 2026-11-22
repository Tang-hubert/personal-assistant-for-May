"""Hexagonal ports. Domain code depends on these; adapters implement them."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.domain.budget import (
    MAX_AUDIO_S,
    MAX_IMAGES,
    MULTIMODAL_TOTAL,
    PromptParts,
    multimodal_estimate,
)
from src.domain.media import AssetVerdict, BatchStatus, MediaBatchView, MediaType
from src.domain.models import (
    BotResponse,
    Channel,
    ChatAction,
    KnowledgeCategory,
    KnowledgeItem,
    NewKnowledgeItem,
    ReplyTarget,
    ResolvedIdentity,
    TaskCapability,
)
from src.domain.session import TopicSessionState, TopicType

Provider = Literal["gemini", "deepseek"]
# overloaded = HTTP 503/504: the model is busy, so a spare model of the same provider may answer.
UnavailableReason = Literal["balance_exhausted", "rate_limited", "overloaded", "provider_error"]


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time, always UTC-aware (ADR-0005)."""
        ...


class ChannelPort(Protocol):
    async def send(self, target: ReplyTarget, response: BotResponse) -> None: ...

    async def send_chat_action(self, target: ReplyTarget, action: ChatAction) -> None: ...


class LLMRequest(BaseModel):
    """One provider call. A request whose prompt, images, or audio would break the
    ARCHITECTURE §4.2 budget cannot be constructed, so it can never be sent.

    When audio_duration_s is set, exactly one of asset_ids is the voice clip; the rest are images.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    user_id: str
    workflow_name: str = Field(max_length=32)
    capability: TaskCapability
    prompt: PromptParts
    asset_ids: tuple[str, ...] = ()
    audio_duration_s: float | None = Field(default=None, gt=0, le=MAX_AUDIO_S)
    max_output_tokens: int = Field(gt=0)
    json_output: bool = False

    @property
    def image_count(self) -> int:
        return len(self.asset_ids) - (1 if self.audio_duration_s is not None else 0)

    @model_validator(mode="after")
    def _check_media_budget(self) -> Self:
        if self.audio_duration_s is not None and not self.asset_ids:
            raise ValueError("an audio request must carry the voice asset in asset_ids")
        if self.image_count > MAX_IMAGES:
            raise ValueError(f"{self.image_count} images, over the {MAX_IMAGES} limit")
        if self.asset_ids:
            estimate = multimodal_estimate(self.prompt, self.image_count, self.audio_duration_s)
            if estimate > MULTIMODAL_TOTAL:
                raise ValueError(f"multimodal prompt is {estimate} tokens, over {MULTIMODAL_TOTAL}")
        return self


class LLMResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    provider: Provider
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str | None = None  # e.g. STOP / MAX_TOKENS, when the provider reports one


class LLMPort(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResult:
        """Raises BudgetExhausted or LLMUnavailable; never a provider SDK error."""
        ...


class BudgetExhausted(Exception):
    """The tenant reached 100% of its Taipei-day budget; no LLM call was made."""


class LLMUnavailable(Exception):
    """A provider refused or failed the call (HTTP 402/429/5xx, transport, or a malformed
    reply). `billed` is the usage of a reply the provider charged for but that was unusable."""

    def __init__(
        self,
        reason: UnavailableReason,
        provider: Provider,
        billed: LLMResult | None = None,
        *,
        detail: str | None = None,
    ) -> None:
        super().__init__(f"{provider}: {reason}" + (f" ({detail})" if detail else ""))
        self.reason: UnavailableReason = reason
        self.provider: Provider = provider
        self.billed = billed
        # A log-safe hint such as "http 503" or "finish=MAX_TOKENS"; never prompt or reply text.
        self.detail = detail


class BudgetStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    EXHAUSTED = "exhausted"


class BudgetGuardPort(Protocol):
    """FinOps Guardian as the router and RoutingLLM see it."""

    async def status(self, tenant_id: str) -> BudgetStatus: ...

    async def ensure_can_spend(self, tenant_id: str) -> None:
        """Raises BudgetExhausted at 100% of the tenant's Taipei-day budget."""
        ...

    async def record(self, request: LLMRequest, result: LLMResult) -> None: ...


class AssetBlob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data: bytes
    mime_type: str


class StoragePort(Protocol):
    async def resolve_identity(
        self, channel: Channel, external_user_id: str
    ) -> ResolvedIdentity | None:
        """The only query not scoped by tenant_id/user_id: it is how they are obtained."""
        ...

    async def save_knowledge(
        self,
        *,
        tenant_id: str,
        user_id: str,
        item: NewKnowledgeItem,
        keywords: Sequence[str] = (),
    ) -> str: ...

    async def get_knowledge(
        self, *, tenant_id: str, user_id: str, item_id: str
    ) -> KnowledgeItem | None: ...

    async def update_knowledge(
        self,
        *,
        tenant_id: str,
        user_id: str,
        item_id: str,
        title: str,
        content_text: str,
        structured_json: dict[str, Any],
        keywords: Sequence[str] = (),
    ) -> bool:
        """Rewrite one item in place and re-index it. False when this owner has no such item."""
        ...

    async def search_knowledge(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str,
        limit: int = 3,
        categories: Sequence[KnowledgeCategory] | None = None,
    ) -> list[KnowledgeItem]:
        """Best FTS matches first; `categories` restricts the hits to those categories."""
        ...


class MediaStorePort(Protocol):
    """Original files plus the media_batches state machine. Scoped by tenant_id and user_id,
    except list_orphan_batches (startup maintenance)."""

    async def save_asset(
        self,
        *,
        tenant_id: str,
        user_id: str,
        data: bytes,
        media_type: MediaType,
        ext: str,
        external_file_id: str | None = None,
    ) -> str: ...

    async def read_asset(self, *, tenant_id: str, user_id: str, asset_id: str) -> bytes | None: ...

    async def read_asset_blob(
        self, *, tenant_id: str, user_id: str, asset_id: str
    ) -> AssetBlob | None:
        """The bytes plus the MIME type implied by the stored file's extension."""
        ...

    async def open_batch(
        self,
        *,
        tenant_id: str,
        user_id: str,
        channel: Channel,
        media_group_id: str | None,
        reply_address: str,
    ) -> str: ...

    async def attach_assets(
        self, *, tenant_id: str, user_id: str, batch_id: str, asset_ids: Sequence[str]
    ) -> None: ...

    async def transition(
        self,
        *,
        tenant_id: str,
        user_id: str,
        batch_id: str,
        frm: BatchStatus,
        to: BatchStatus,
        intent_expires_at: datetime | None = None,
    ) -> bool:
        """Conditional move; True only for the caller that changed the row. Illegal pairs raise."""
        ...

    async def save_gate_result(
        self,
        *,
        tenant_id: str,
        user_id: str,
        batch_id: str,
        verdicts: Sequence[AssetVerdict],
    ) -> None: ...

    async def get_batch(
        self, *, tenant_id: str, user_id: str, batch_id: str
    ) -> MediaBatchView | None: ...

    async def batch_asset_ids(
        self, *, tenant_id: str, user_id: str, batch_id: str
    ) -> list[str]: ...

    async def kept_asset_ids(self, *, tenant_id: str, user_id: str, batch_id: str) -> list[str]: ...

    async def list_orphan_batches(self) -> list[MediaBatchView]: ...


class OpsNotifierPort(Protocol):
    async def notify(
        self, level: Literal["warning", "critical", "error"], message: str
    ) -> None: ...


class LedgerPort(Protocol):
    """llm_usage_ledger plus the tenant budget columns. spent_between, daily_budget, and
    claim_alert_80 are tenant-level aggregates: the third documented exception to
    user_id scoping (ARCHITECTURE §5)."""

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
    ) -> None: ...

    async def spent_between(self, tenant_id: str, start: datetime, end: datetime) -> Decimal:
        """Sum of estimated_cost_usd over the half-open UTC range [start, end)."""
        ...

    async def daily_budget(self, tenant_id: str) -> Decimal: ...

    async def claim_alert_80(self, tenant_id: str, day: date) -> bool:
        """True only for the one caller that marked `day` as alerted."""
        ...


class SessionStorePort(Protocol):
    async def get_active(self, *, tenant_id: str, user_id: str) -> TopicSessionState | None: ...

    async def open_fresh(
        self,
        *,
        tenant_id: str,
        user_id: str,
        topic_type: TopicType,
        state_json: dict[str, Any] | None = None,
    ) -> TopicSessionState:
        """Close any active session and open an empty one; the result is the active session."""
        ...

    async def save(self, *, tenant_id: str, user_id: str, state: TopicSessionState) -> bool:
        """Persist a still-active session. False when it was closed meanwhile."""
        ...

    async def close(self, *, tenant_id: str, user_id: str, session_id: str) -> None: ...
