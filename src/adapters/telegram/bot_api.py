"""The few Bot API calls the adapter makes, behind a Protocol so tests never reach Telegram."""

from __future__ import annotations

from typing import Protocol

from telegram import Bot, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ParseMode

from src.domain.models import ChatAction


class BotApi(Protocol):
    async def get_file_bytes(self, file_id: str) -> bytes: ...

    async def send_message(
        self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None
    ) -> None: ...

    async def send_photo(
        self, chat_id: int, photo: bytes, caption: str, markup: InlineKeyboardMarkup | None
    ) -> None: ...

    async def send_chat_action(self, chat_id: int, action: ChatAction) -> None: ...

    async def answer_callback_query(self, callback_query_id: str) -> None: ...


class PtbBotApi:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def get_file_bytes(self, file_id: str) -> bytes:
        file = await self._bot.get_file(file_id)
        return bytes(await file.download_as_bytearray())

    async def send_message(
        self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None
    ) -> None:
        await self._bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def send_photo(
        self, chat_id: int, photo: bytes, caption: str, markup: InlineKeyboardMarkup | None
    ) -> None:
        await self._bot.send_photo(
            chat_id, photo, caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup
        )

    async def send_chat_action(self, chat_id: int, action: ChatAction) -> None:
        await self._bot.send_chat_action(chat_id, action)

    async def answer_callback_query(self, callback_query_id: str) -> None:
        await self._bot.answer_callback_query(callback_query_id)
