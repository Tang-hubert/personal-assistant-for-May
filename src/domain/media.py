"""Photo-burst domain: batch state machine (ARCHITECTURE §4.6), intents, and the fixed
BotResponses May sees. No I/O and no image library lives here."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from src.domain.models import ActionButton, BotResponse, Channel

MediaType = Literal["image", "audio"]

INTENT_TTL = timedelta(minutes=15)
MAX_BATCH_IMAGES = 6


class BatchStatus(StrEnum):
    COLLECTING = "collecting"
    FILTERED = "filtered"
    AWAITING_INTENT = "awaiting_intent"
    DONE = "done"


ALLOWED_TRANSITIONS: frozenset[tuple[BatchStatus, BatchStatus]] = frozenset(
    {
        (BatchStatus.COLLECTING, BatchStatus.FILTERED),
        (BatchStatus.FILTERED, BatchStatus.AWAITING_INTENT),
        (BatchStatus.FILTERED, BatchStatus.DONE),
        (BatchStatus.AWAITING_INTENT, BatchStatus.DONE),
    }
)


class Intent(StrEnum):
    LECTURE = "lecture"
    SALES = "sales"
    MEMORY = "memory"
    IOS = "ios"


INTENT_LABELS: dict[Intent, str] = {
    Intent.LECTURE: "📚 整理講座重點",
    Intent.SALES: "💎 寫珠寶推銷文案",
    Intent.MEMORY: "📦 記下東西放哪了",
    Intent.IOS: "📱 教我用手機",
}

_CALLBACK_PREFIX = "b"


def encode_callback(batch_id: str, intent: Intent) -> str:
    return f"{_CALLBACK_PREFIX}:{batch_id}:{intent.value}"


def parse_callback(data: str) -> tuple[str, Intent] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != _CALLBACK_PREFIX or not parts[1]:
        return None
    try:
        return parts[1], Intent(parts[2])
    except ValueError:
        return None


class _Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AssetVerdict(_Value):
    asset_id: str
    phash: str | None
    blur_score: float | None
    is_blurry: bool
    is_kept: bool


class MediaBatchView(_Value):
    id: str
    tenant_id: str
    user_id: str
    channel: Channel
    media_group_id: str | None
    status: BatchStatus
    raw_count: int
    kept_count: int
    reply_address: str | None
    intent_expires_at: AwareDatetime | None


class IntentSelected(_Value):
    """Handed to the T4 workflow once May taps a button on a live batch."""

    batch_id: str
    intent: Intent
    kept_asset_ids: tuple[str, ...]


def intent_prompt(batch_id: str, kept_count: int) -> BotResponse:
    return BotResponse(
        title="照片收到囉",
        bullets=(f"我挑了 {kept_count} 張比較清楚的", "請點下面的按鈕告訴我要做什麼"),
        actions=tuple(
            ActionButton(
                label=INTENT_LABELS[intent], callback_data=encode_callback(batch_id, intent)
            )
            for intent in Intent
        ),
    )


def retake_prompt() -> BotResponse:
    return BotResponse(
        title="照片有點模糊",
        bullets=("這次的照片都不太清楚", "請拿穩、靠近一點、再拍一次"),
    )


def already_done() -> BotResponse:
    return BotResponse(title="已經處理好囉", bullets=("這批照片已經處理完了",))


def resend_prompt() -> BotResponse:
    return BotResponse(
        title="這批照片過期了",
        bullets=("請再傳一次照片給我", "我會重新幫您看"),
    )
