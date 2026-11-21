"""Telegram Update -> what May sent, before any I/O. Pure: no download, no DB."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Literal

from telegram import Message, Update, Voice
from telegram.constants import ChatType

# Documents May sends from the iPhone Files app; the MIME type picks the stored extension.
IMAGE_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
}
IMAGE_EXT_BY_SUFFIX = {".heic": "heic", ".heif": "heif"}


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Media:
    kind: Literal["image", "audio"]
    file_id: str
    file_size: int | None
    ext: str
    duration_s: float | None = None
    media_group_id: str | None = None


@dataclass(frozen=True)
class Callback:
    data: str


@dataclass(frozen=True)
class Unsupported:
    pass


Content = Text | Media | Callback | Unsupported


@dataclass(frozen=True)
class Envelope:
    event_id: str
    user_id: int
    chat_id: int
    content: Content
    callback_query_id: str | None = None


def parse_update(update: Update) -> Envelope | None:
    """None for anything that is not a private message or a button tap."""
    if (query := update.callback_query) is not None:
        if query.message is not None and query.message.chat.type != ChatType.PRIVATE:
            return None
        return Envelope(
            event_id=f"tg:cb:{query.id}",
            user_id=query.from_user.id,
            # The tapped message can be too old for Telegram to include; the user's id is
            # the private chat's id.
            chat_id=query.message.chat.id if query.message is not None else query.from_user.id,
            content=Callback(query.data) if query.data else Unsupported(),
            callback_query_id=query.id,
        )
    message = update.message
    if message is None or message.from_user is None or message.chat.type != ChatType.PRIVATE:
        return None
    return Envelope(
        event_id=f"tg:u:{update.update_id}",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        content=_content(message),
    )


def _content(message: Message) -> Content:
    if message.text is not None:
        return Text(message.text)
    if message.photo:
        largest = message.photo[-1]
        return Media(
            "image", largest.file_id, largest.file_size, "jpg", None, message.media_group_id
        )
    if (document := message.document) is not None:
        ext = IMAGE_EXT_BY_MIME.get(document.mime_type or "") or IMAGE_EXT_BY_SUFFIX.get(
            PurePosixPath(document.file_name or "").suffix.lower()
        )
        if ext is not None:
            return Media(
                "image", document.file_id, document.file_size, ext, None, message.media_group_id
            )
    if (voice := message.voice) is not None:
        return Media("audio", voice.file_id, voice.file_size, "ogg", _seconds(voice))
    return Unsupported()


def _seconds(voice: Voice) -> float:
    # PTB 22 returns int seconds with a deprecation warning unless PTB_TIMEDELTA is set.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        duration = voice.duration
    return duration.total_seconds() if isinstance(duration, timedelta) else float(duration)
