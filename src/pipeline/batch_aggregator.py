"""Collapses a photo burst into one media_batches row and one BotResponse (T2).

No LLMPort is accepted anywhere here: the burst path is zero-token by construction.

Delivery is at-least-once. The response is sent before the batch leaves `filtered`, so a crash
between the two re-sends that one response during recover_orphans (ARCHITECTURE §4.6) instead of
losing it. Status changes go only through MediaStorePort.transition, a conditional UPDATE, so a
finalize that lost a race, or a second recovery run, does nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Coroutine
from dataclasses import asdict, dataclass, field
from typing import Any

from src.domain.media import (
    INTENT_TTL,
    BatchStatus,
    Intent,
    IntentSelected,
    already_done,
    intent_already_done,
    intent_prompt,
    parse_callback,
    resend_prompt,
    retake_prompt,
)
from src.domain.models import BotResponse, IncomingEvent, ReplyTarget
from src.domain.ports import ChannelPort, Clock, MediaStorePort
from src.pipeline.image_gate import evaluate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Scope:
    tenant_id: str
    user_id: str
    batch_id: str


@dataclass
class _Collecting:
    scope: _Scope
    target: ReplyTarget
    group_seen: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    timer: asyncio.Task[None] | None = None


class BatchAggregator:
    def __init__(
        self,
        store: MediaStorePort,
        channel: ChannelPort,
        clock: Clock,
        *,
        blur_threshold: float,
        debounce_s: float = 5.0,
    ) -> None:
        self._store = store
        self._channel = channel
        self._clock = clock
        self._blur_threshold = blur_threshold
        self._debounce_s = debounce_s
        self._collecting: dict[tuple[str, str], _Collecting] = {}
        self._locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task[None]] = set()

    async def add(self, event: IncomingEvent) -> None:
        """Join the user's collecting batch, or open one. The batch closes after `debounce_s`
        of quiet, or at once when a media group has delivered `group_total` images."""
        if event.event_type != "image":
            raise ValueError(f"BatchAggregator only takes image events, got {event.event_type}")
        key = (event.tenant_id, event.user_id)
        async with self._locks[key]:
            entry = self._collecting.get(key)
            if entry is None:
                batch_id = await self._store.open_batch(
                    tenant_id=event.tenant_id,
                    user_id=event.user_id,
                    channel=event.channel,
                    media_group_id=event.media_group_id,
                    reply_address=event.reply_target.address,
                )
                entry = _Collecting(
                    _Scope(event.tenant_id, event.user_id, batch_id), event.reply_target
                )
                self._collecting[key] = entry
            await self._store.attach_assets(**asdict(entry.scope), asset_ids=event.asset_ids)
            # The newest event's target may carry a still-valid reply token (LINE); keep all of it.
            entry.target = event.reply_target

            group_done = False
            if event.media_group_id is not None and event.group_total is not None:
                entry.group_seen[event.media_group_id] += len(event.asset_ids)
                group_done = entry.group_seen[event.media_group_id] >= event.group_total

            if entry.timer is not None:
                entry.timer.cancel()
            if group_done:
                del self._collecting[key]
                self._spawn(self._finalize(entry))
            else:
                entry.timer = self._spawn(self._close_after_quiet(key, entry))

    async def drain(self) -> None:
        """Wait for every pending debounce timer and finalize. For shutdown and tests."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def recover_orphans(self) -> int:
        """Finish batches a restart left in collecting or filtered. Call once at startup,
        before events are accepted. Returns how many batches were completed."""
        recovered = 0
        for batch in await self._store.list_orphan_batches():
            if batch.reply_address is None:
                logger.warning("orphan batch %s has no reply address; skipped", batch.id)
                continue
            scope = _Scope(batch.tenant_id, batch.user_id, batch.id)
            target = ReplyTarget(address=batch.reply_address)
            try:
                if batch.status is BatchStatus.COLLECTING and not await self._store.transition(
                    **asdict(scope), frm=BatchStatus.COLLECTING, to=BatchStatus.FILTERED
                ):
                    continue
                await self._gate_and_respond(scope, target)
            except Exception:
                logger.exception("recovering batch %s failed; it stays recoverable", batch.id)
                continue
            recovered += 1
        return recovered

    async def handle_intent_callback(self, event: IncomingEvent) -> IntentSelected | BotResponse:
        """Resolve a button tap. Concurrent double taps are serialized by the T3 per-user lock.
        A finished intent answers "already done" for good; any other intent may still run
        within the TTL, because a finished workflow leaves the batch awaiting_intent."""
        parsed = parse_callback(event.callback_data or "")
        if parsed is None:
            return resend_prompt()
        batch_id, intent = parsed
        batch = await self._store.get_batch(
            tenant_id=event.tenant_id, user_id=event.user_id, batch_id=batch_id
        )
        if batch is None:
            return resend_prompt()
        if batch.status is BatchStatus.DONE:  # all blurry, or finished before T5.1
            return already_done()
        if intent in batch.completed_intents:
            return intent_already_done()
        if batch.status is not BatchStatus.AWAITING_INTENT:
            return resend_prompt()
        if batch.intent_expires_at is None or self._clock.now() >= batch.intent_expires_at:
            return resend_prompt()
        kept = await self._store.kept_asset_ids(
            tenant_id=event.tenant_id, user_id=event.user_id, batch_id=batch_id
        )
        return IntentSelected(batch_id=batch_id, intent=intent, kept_asset_ids=tuple(kept))

    async def complete(
        self, *, tenant_id: str, user_id: str, batch_id: str, intent: Intent
    ) -> bool:
        """Called by a workflow when it has finished. False if that intent was already
        finished or the batch was not awaiting."""
        return await self._store.mark_intent_done(
            tenant_id=tenant_id, user_id=user_id, batch_id=batch_id, intent=intent
        )

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._reap)
        return task

    def _reap(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("batch finalize failed; recovery will retry it", exc_info=error)

    async def _close_after_quiet(self, key: tuple[str, str], entry: _Collecting) -> None:
        await asyncio.sleep(self._debounce_s)
        async with self._locks[key]:
            if self._collecting.get(key) is not entry:
                return
            del self._collecting[key]
        await self._finalize(entry)

    async def _finalize(self, entry: _Collecting) -> None:
        moved = await self._store.transition(
            **asdict(entry.scope), frm=BatchStatus.COLLECTING, to=BatchStatus.FILTERED
        )
        if moved:
            await self._gate_and_respond(entry.scope, entry.target)

    async def _gate_and_respond(self, scope: _Scope, target: ReplyTarget) -> None:
        asset_ids = await self._store.batch_asset_ids(**asdict(scope))
        images = [
            (
                asset_id,
                await self._store.read_asset(
                    tenant_id=scope.tenant_id, user_id=scope.user_id, asset_id=asset_id
                )
                or b"",
            )
            for asset_id in asset_ids
        ]
        result = await asyncio.to_thread(evaluate, images, blur_threshold=self._blur_threshold)
        await self._store.save_gate_result(**asdict(scope), verdicts=result.verdicts)

        if result.kept_ids:
            response = intent_prompt(scope.batch_id, len(result.kept_ids))
            next_status = BatchStatus.AWAITING_INTENT
        else:
            response = retake_prompt()
            next_status = BatchStatus.DONE
        await self._channel.send(target, response)
        await self._store.transition(
            **asdict(scope),
            frm=BatchStatus.FILTERED,
            to=next_status,
            intent_expires_at=(
                self._clock.now() + INTENT_TTL
                if next_status is BatchStatus.AWAITING_INTENT
                else None
            ),
        )
