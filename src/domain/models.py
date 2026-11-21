"""Channel-agnostic contracts (ARCHITECTURE §2). Every limit is a validator, not a convention."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field, model_validator

Channel = Literal["telegram", "line"]
EventType = Literal["text", "audio", "image", "action_callback", "unsupported"]
ChatAction = Literal["typing", "upload_photo"]
Priority = Literal["reply", "proactive_critical", "proactive_digest"]
KnowledgeCategory = Literal["jewelry", "product", "lecture", "item_location", "crm"]

ALT_TEXT_MAX_CHARS = 400
CALLBACK_DATA_MAX_BYTES = 64


class TaskCapability(StrEnum):
    MULTIMODAL_IMAGE = "multimodal_image"
    MULTIMODAL_AUDIO = "multimodal_audio"
    COPYWRITING_TW = "copywriting_tw"
    TEXT_FAST_ROUTE = "text_fast_route"


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ReplyTarget(_Contract):
    """Read only by adapters (ADR-0002)."""

    address: str
    ephemeral_token: str | None = None
    token_expires_at: AwareDatetime | None = None


class IncomingEvent(_Contract):
    event_id: str
    tenant_id: str
    user_id: str
    channel: Channel
    event_type: EventType
    reply_target: ReplyTarget
    payload_text: str | None = None
    callback_data: str | None = None
    asset_ids: tuple[str, ...] = ()
    media_group_id: str | None = None
    group_index: int | None = None
    group_total: int | None = None
    audio_duration_s: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if (self.group_index is None) != (self.group_total is None):
            raise ValueError("group_index and group_total must be given together")
        if (
            self.group_index is not None
            and self.group_total is not None
            and not 1 <= self.group_index <= self.group_total
        ):
            raise ValueError("group_index must be within 1..group_total")
        if self.event_type in ("image", "audio") and not self.asset_ids:
            raise ValueError(f"{self.event_type} events must carry asset_ids")
        if self.event_type == "audio" and self.audio_duration_s is None:
            raise ValueError("audio events must carry audio_duration_s from platform metadata")
        if self.event_type == "action_callback" and not self.callback_data:
            raise ValueError("action_callback events must carry callback_data")
        return self


class CopyableBlock(_Contract):
    label: str
    body_text: str = Field(max_length=1000)
    warning_note: str | None = None


class ActionButton(_Contract):
    label: str = Field(max_length=20)
    callback_data: str
    prefer_voice_input: bool = False

    @model_validator(mode="after")
    def _check_callback_bytes(self) -> Self:
        if len(self.callback_data.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
            raise ValueError(f"callback_data exceeds {CALLBACK_DATA_MAX_BYTES} UTF-8 bytes")
        return self


class BotResponse(_Contract):
    title: str = Field(max_length=30)
    bullets: tuple[str, ...] = Field(default=(), max_length=3)
    copyable_blocks: tuple[CopyableBlock, ...] = ()
    actions: tuple[ActionButton, ...] = Field(default=(), max_length=4)
    image_asset_id: str | None = None
    priority: Priority = "reply"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def alt_text(self) -> str:
        return "\n".join((self.title, *self.bullets))[:ALT_TEXT_MAX_CHARS]


class ResolvedIdentity(_Contract):
    tenant_id: str
    user_id: str


class NewKnowledgeItem(_Contract):
    category: KnowledgeCategory
    title: str
    content_text: str
    structured_json: dict[str, Any] = Field(default_factory=dict)
    primary_asset_id: str | None = None


class KnowledgeItem(NewKnowledgeItem):
    id: str
    tenant_id: str
    user_id: str
    created_at: AwareDatetime
