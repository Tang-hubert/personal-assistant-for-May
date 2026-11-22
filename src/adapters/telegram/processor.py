"""PTB update processor: one user's updates run one at a time in Telegram order, different users
run side by side, and a button tap is acknowledged before it waits in its user's queue.

With PTB's default (one update at a time for everyone) a tap queued behind a slow Gemini call
was answered after Telegram's answer window and failed with "query is too old".

Order holds because PTB starts one task per update in Telegram order and nothing here awaits
before the per-user lock is requested; asyncio locks and semaphores wake waiters FIFO.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import Update
from telegram.ext import BaseUpdateProcessor

logger = logging.getLogger(__name__)

MAX_CONCURRENT_UPDATES = 16

Acknowledge = Callable[[object], Awaitable[None]]


class PerUserUpdateProcessor(BaseUpdateProcessor):
    def __init__(
        self, acknowledge: Acknowledge, max_concurrent_updates: int = MAX_CONCURRENT_UPDATES
    ) -> None:
        super().__init__(max_concurrent_updates)
        self._acknowledge = acknowledge
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._acks: set[asyncio.Task[None]] = set()

    async def do_process_update(self, update: object, coroutine: Awaitable[Any]) -> None:
        user_id = 0
        if isinstance(update, Update):
            if update.callback_query is not None:
                task = asyncio.create_task(self._ack(update))
                self._acks.add(task)
                task.add_done_callback(self._acks.discard)
            if update.effective_user is not None:
                user_id = update.effective_user.id
        async with self._locks[user_id]:
            await coroutine

    async def _ack(self, update: Update) -> None:
        try:
            await self._acknowledge(update)
        except Exception as err:
            logger.warning("callback acknowledge failed: %s", type(err).__name__)

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        if self._acks:
            await asyncio.gather(*self._acks, return_exceptions=True)
