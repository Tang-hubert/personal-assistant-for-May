"""Hexagonal ports. Domain code depends on these; adapters implement them."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

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


class OpsNotifierPort(Protocol):
    async def notify(
        self, level: Literal["warning", "critical", "error"], message: str
    ) -> None: ...
