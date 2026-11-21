"""Telegram long-polling edge (ARCHITECTURE §2). All Telegram-specific guards live here:
allowlist, private chats only, duplicate deliveries, file size and voice length before any
download, and the one-message-per-event rule (ADR-0002) even when something fails."""

from __future__ import annotations

import asyncio
import io
import logging
import traceback
from collections.abc import AsyncIterator, Set
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from typing import Protocol

import pillow_heif
from PIL import Image
from telegram import Update

from src.adapters.telegram.bot_api import BotApi
from src.adapters.telegram.inbound import Callback, Envelope, Media, Text, parse_update
from src.adapters.telegram.render import CAPTION_MAX, MESSAGE_MAX, render
from src.domain import replies
from src.domain.models import (
    BotResponse,
    ChatAction,
    EventType,
    IncomingEvent,
    ReplyTarget,
    ResolvedIdentity,
)
from src.domain.ports import Clock, MediaStorePort, OpsNotifierPort, StoragePort
from src.pipeline.event_dedup import RecentEventIds

pillow_heif.register_heif_opener()

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024
MAX_PHOTO_UPLOAD_BYTES = 10 * 1024 * 1024
TELEGRAM_PHOTO_MIMES = frozenset({"image/jpeg", "image/png"})
TYPING_INTERVAL_S = 4.5
JPEG_QUALITY = 85
# A burst is many updates; photos that never reach the aggregator share one notice.
PHOTO_NOTICE_WINDOW = timedelta(seconds=60)


class EventHandler(Protocol):
    async def handle(self, event: IncomingEvent) -> BotResponse | None: ...


def _frames(err: BaseException) -> str:
    # File and line only: an exception's message can carry user text or a request URL.
    return "".join(traceback.format_tb(err.__traceback__))


def to_jpeg(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


class TelegramChannel:
    """ChannelPort. Only private chats are accepted, so a chat id is the user's Telegram id."""

    def __init__(self, bot: BotApi, storage: StoragePort, media: MediaStorePort) -> None:
        self._bot = bot
        self._storage = storage
        self._media = media

    async def send(self, target: ReplyTarget, response: BotResponse) -> None:
        chat_id = int(target.address)
        try:
            photo = await self._photo(target, response.image_asset_id)
            if photo is not None:
                rendered = render(response, limit=CAPTION_MAX)
                await self._bot.send_photo(chat_id, photo, rendered.text, rendered.markup)
                return
        except Exception as err:
            logger.warning("photo reply failed, sending text only: %s", type(err).__name__)
        rendered = render(response, limit=MESSAGE_MAX)
        await self._bot.send_message(chat_id, rendered.text, rendered.markup)

    async def send_chat_action(self, target: ReplyTarget, action: ChatAction) -> None:
        await self._bot.send_chat_action(int(target.address), action)

    async def _photo(self, target: ReplyTarget, asset_id: str | None) -> bytes | None:
        if asset_id is None:
            return None
        owner = await self._storage.resolve_identity("telegram", target.address)
        if owner is None:
            return None
        blob = await self._media.read_asset_blob(
            tenant_id=owner.tenant_id, user_id=owner.user_id, asset_id=asset_id
        )
        if blob is None:
            logger.warning("asset %s is missing; replying without the photo", asset_id)
            return None
        if blob.mime_type in TELEGRAM_PHOTO_MIMES and len(blob.data) <= MAX_PHOTO_UPLOAD_BYTES:
            return blob.data
        return await asyncio.to_thread(to_jpeg, blob.data)


class TelegramAdapter:
    def __init__(
        self,
        *,
        bot: BotApi,
        channel: TelegramChannel,
        router: EventHandler,
        storage: StoragePort,
        media: MediaStorePort,
        notifier: OpsNotifierPort,
        allowed_user_ids: Set[int],
        clock: Clock,
        dedup: RecentEventIds | None = None,
        typing_interval_s: float = TYPING_INTERVAL_S,
    ) -> None:
        self._bot = bot
        self._channel = channel
        self._router = router
        self._storage = storage
        self._media = media
        self._notifier = notifier
        self._allowed = allowed_user_ids
        self._clock = clock
        self._photo_notice_at: dict[str, datetime] = {}
        self._dedup = dedup or RecentEventIds()
        self._typing_interval_s = typing_interval_s

    async def handle_update(self, update: Update) -> None:
        envelope = parse_update(update)
        if envelope is None:
            return
        if envelope.user_id not in self._allowed:
            logger.info(
                "ignored a message from telegram user %s (not allowlisted)", envelope.user_id
            )
            return
        if not self._dedup.claim(envelope.event_id):
            return
        if envelope.callback_query_id is not None:
            try:
                await self._bot.answer_callback_query(envelope.callback_query_id)
            except Exception as err:
                # A tap older than Telegram's answer window still deserves its reply.
                logger.warning("callback answer failed: %s", type(err).__name__)
        target = ReplyTarget(address=str(envelope.chat_id))
        try:
            response = await self._respond(envelope, target)
        except Exception as err:
            logger.error(
                "update %s failed: %s\n%s", envelope.event_id, type(err).__name__, _frames(err)
            )
            await self._notifier.notify("error", f"telegram update failed: {type(err).__name__}")
            response = replies.busy_message()
        if response is None:
            return
        try:
            await self._channel.send(target, response)
        except Exception as err:
            logger.error(
                "reply to %s not sent: %s\n%s", envelope.event_id, type(err).__name__, _frames(err)
            )
            await self._notifier.notify("error", f"telegram reply not sent: {type(err).__name__}")

    async def _respond(self, envelope: Envelope, target: ReplyTarget) -> BotResponse | None:
        identity = await self._storage.resolve_identity("telegram", str(envelope.user_id))
        if identity is None:
            logger.warning(
                "allowlisted telegram user %s has no identity; restart seeds it", envelope.user_id
            )
            return None
        content = envelope.content
        if isinstance(content, Media):
            if content.kind == "audio" and (
                rejection := replies.voice_rejection(content.duration_s or 0.0)
            ):
                return rejection
            if content.kind == "image":
                return await self._image(envelope, identity, target, content)
            if too_large(content):
                return replies.file_too_large()
        async with self._typing(target):
            match content:
                case Text(text=text):
                    event = self._event(envelope, identity, target, "text", payload_text=text)
                case Callback(data=data):
                    event = self._event(
                        envelope, identity, target, "action_callback", callback_data=data
                    )
                case Media():
                    event = await self._media_event(envelope, identity, target, content)
                case _:
                    event = self._event(envelope, identity, target, "unsupported")
            return await self._router.handle(event)

    async def _image(
        self, envelope: Envelope, identity: ResolvedIdentity, target: ReplyTarget, media: Media
    ) -> BotResponse | None:
        # No chat action: the aggregator answers the whole burst once, later.
        if too_large(media):
            return self._photo_notice(target, replies.file_too_large())
        try:
            event = await self._media_event(envelope, identity, target, media)
        except Exception as err:
            logger.error(
                "photo %s not received: %s\n%s", envelope.event_id, type(err).__name__, _frames(err)
            )
            notice = self._photo_notice(target, replies.photo_not_received())
            if notice is not None:  # one alert per burst, like the notice May gets
                await self._notifier.notify(
                    "error", f"telegram photo not received: {type(err).__name__}"
                )
            return notice
        return await self._router.handle(event)

    def _photo_notice(self, target: ReplyTarget, notice: BotResponse) -> BotResponse | None:
        now = self._clock.now()
        last = self._photo_notice_at.get(target.address)
        if last is not None and now - last < PHOTO_NOTICE_WINDOW:
            return None
        self._photo_notice_at[target.address] = now
        return notice

    async def _media_event(
        self, envelope: Envelope, identity: ResolvedIdentity, target: ReplyTarget, media: Media
    ) -> IncomingEvent:
        data = await self._bot.get_file_bytes(media.file_id)
        asset_id = await self._media.save_asset(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            data=data,
            media_type=media.kind,
            ext=media.ext,
            external_file_id=media.file_id,
        )
        return self._event(
            envelope,
            identity,
            target,
            media.kind,
            asset_ids=(asset_id,),
            media_group_id=media.media_group_id,
            audio_duration_s=media.duration_s,
        )

    def _event(
        self,
        envelope: Envelope,
        identity: ResolvedIdentity,
        target: ReplyTarget,
        event_type: EventType,
        *,
        payload_text: str | None = None,
        callback_data: str | None = None,
        asset_ids: tuple[str, ...] = (),
        media_group_id: str | None = None,
        audio_duration_s: float | None = None,
    ) -> IncomingEvent:
        return IncomingEvent(
            event_id=envelope.event_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            channel="telegram",
            event_type=event_type,
            reply_target=target,
            payload_text=payload_text,
            callback_data=callback_data,
            asset_ids=asset_ids,
            media_group_id=media_group_id,
            audio_duration_s=audio_duration_s,
        )

    @asynccontextmanager
    async def _typing(self, target: ReplyTarget) -> AsyncIterator[None]:
        task = asyncio.create_task(self._keep_typing(target))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _keep_typing(self, target: ReplyTarget) -> None:
        while True:
            try:
                await self._channel.send_chat_action(target, "typing")
            except Exception as err:
                logger.warning("typing action failed: %s", type(err).__name__)
            await asyncio.sleep(self._typing_interval_s)


def too_large(media: Media) -> bool:
    """Unknown sizes download; Telegram's own limits bound them."""
    return media.file_size is not None and media.file_size > MAX_DOWNLOAD_BYTES
