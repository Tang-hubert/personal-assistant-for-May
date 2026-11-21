"""Hexagonal ports. Domain code depends on these; adapters implement them."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from src.domain.media import AssetVerdict, BatchStatus, MediaBatchView, MediaType
from src.domain.models import (
    BotResponse,
    Channel,
    ChatAction,
    KnowledgeItem,
    NewKnowledgeItem,
    ReplyTarget,
    ResolvedIdentity,
    TaskCapability,
)


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time, always UTC-aware (ADR-0005)."""
        ...


class ChannelPort(Protocol):
    async def send(self, target: ReplyTarget, response: BotResponse) -> None: ...

    async def send_chat_action(self, target: ReplyTarget, action: ChatAction) -> None: ...


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: TaskCapability
    system: str
    user_text: str
    asset_ids: tuple[str, ...] = ()
    max_output_tokens: int


class LLMResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    provider: Literal["gemini", "deepseek"]
    model: str
    prompt_tokens: int
    completion_tokens: int


class LLMPort(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResult: ...


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

    async def search_knowledge(
        self, *, tenant_id: str, user_id: str, query: str, limit: int = 3
    ) -> list[KnowledgeItem]: ...


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
