"""SQLite implementation of MediaStorePort. Every query is scoped by tenant_id and user_id,
except list_orphan_batches(): startup recovery must see every tenant's stranded batches.
That is the second documented unscoped query, after identity resolution (ARCHITECTURE §5)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from src.adapters.storage.orm import MediaAsset, MediaBatch, new_id
from src.domain.media import (
    ALLOWED_TRANSITIONS,
    AssetVerdict,
    BatchStatus,
    Intent,
    MediaBatchView,
    MediaType,
)
from src.domain.models import Channel
from src.domain.ports import AssetBlob, Clock

MEDIA_DIR = "media"

# An explicit table: the stdlib mimetypes module reads the Windows registry, so its answers
# differ between the dev machine and the Linux server.
MIME_BY_EXT: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "mp3": "audio/mp3",
    "m4a": "audio/mp4",
    "wav": "audio/wav",
}


def _view(row: MediaBatch) -> MediaBatchView:
    return MediaBatchView(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        channel=row.channel,
        media_group_id=row.media_group_id,
        status=BatchStatus(row.status),
        raw_count=row.raw_count,
        kept_count=row.kept_count,
        reply_address=row.reply_address,
        intent_expires_at=row.intent_expires_at,
        completed_intents=tuple(Intent(value) for value in row.completed_intents),
    )


class SqlMediaStore:
    def __init__(self, engine: AsyncEngine, clock: Clock, data_dir: Path) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._clock = clock
        self._data_dir = data_dir

    async def save_asset(
        self,
        *,
        tenant_id: str,
        user_id: str,
        data: bytes,
        media_type: MediaType,
        ext: str,
        external_file_id: str | None = None,
    ) -> str:
        """Write the original file, then record it with no batch yet (batch_id NULL)."""
        if not ext.isalnum():
            raise ValueError(f"unsafe file extension: {ext!r}")
        asset_id = new_id()
        relative = PurePosixPath(MEDIA_DIR, tenant_id, f"{asset_id}.{ext.lower()}")
        target = self._data_dir.joinpath(*relative.parts)

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        await asyncio.to_thread(_write)
        async with self._sessions() as session:
            session.add(
                MediaAsset(
                    id=asset_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    external_file_id=external_file_id,
                    media_type=media_type,
                    local_path=relative.as_posix(),
                    created_at=self._clock.now(),
                )
            )
            await session.commit()
        return asset_id

    async def read_asset(self, *, tenant_id: str, user_id: str, asset_id: str) -> bytes | None:
        """None when the asset is not this owner's, or its file is gone from disk."""
        loaded = await self._load(tenant_id, user_id, asset_id)
        return None if loaded is None else loaded[1]

    async def read_asset_blob(
        self, *, tenant_id: str, user_id: str, asset_id: str
    ) -> AssetBlob | None:
        loaded = await self._load(tenant_id, user_id, asset_id)
        if loaded is None:
            return None
        local_path, data = loaded
        suffix = PurePosixPath(local_path).suffix.lstrip(".").lower()
        return AssetBlob(data=data, mime_type=MIME_BY_EXT[suffix])

    async def _load(self, tenant_id: str, user_id: str, asset_id: str) -> tuple[str, bytes] | None:
        async with self._sessions() as session:
            local_path = (
                await session.execute(
                    select(MediaAsset.local_path).where(
                        MediaAsset.id == asset_id,
                        MediaAsset.tenant_id == tenant_id,
                        MediaAsset.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
        if local_path is None:
            return None
        path = self._data_dir.joinpath(*PurePosixPath(local_path).parts)
        try:
            return local_path, await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            return None

    async def open_batch(
        self,
        *,
        tenant_id: str,
        user_id: str,
        channel: Channel,
        media_group_id: str | None,
        reply_address: str,
    ) -> str:
        async with self._sessions() as session:
            row = MediaBatch(
                tenant_id=tenant_id,
                user_id=user_id,
                channel=channel,
                media_group_id=media_group_id,
                status=BatchStatus.COLLECTING.value,
                reply_address=reply_address,
                created_at=self._clock.now(),
            )
            session.add(row)
            await session.commit()
            return row.id

    async def attach_assets(
        self, *, tenant_id: str, user_id: str, batch_id: str, asset_ids: Sequence[str]
    ) -> None:
        """Claim this owner's unattached assets for the batch; each counts toward raw_count once."""
        async with self._sessions() as session:
            claimed = await session.execute(
                update(MediaAsset)
                .where(
                    MediaAsset.id.in_(asset_ids),
                    MediaAsset.tenant_id == tenant_id,
                    MediaAsset.user_id == user_id,
                    MediaAsset.batch_id.is_(None),
                )
                .values(batch_id=batch_id)
            )
            await session.execute(
                update(MediaBatch)
                .where(
                    MediaBatch.id == batch_id,
                    MediaBatch.tenant_id == tenant_id,
                    MediaBatch.user_id == user_id,
                )
                .values(raw_count=MediaBatch.raw_count + claimed.rowcount)  # type: ignore[attr-defined]
            )
            await session.commit()

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
        """The only way a batch changes status. True only for the caller that won the move."""
        if (frm, to) not in ALLOWED_TRANSITIONS:
            raise ValueError(f"illegal batch transition {frm.value} -> {to.value}")
        values: dict[str, object] = {"status": to.value}
        if intent_expires_at is not None:
            values["intent_expires_at"] = intent_expires_at
        async with self._sessions() as session:
            result = await session.execute(
                update(MediaBatch)
                .where(
                    MediaBatch.id == batch_id,
                    MediaBatch.tenant_id == tenant_id,
                    MediaBatch.user_id == user_id,
                    MediaBatch.status == frm.value,
                )
                .values(**values)
            )
            await session.commit()
            return result.rowcount == 1  # type: ignore[attr-defined,no-any-return]

    async def mark_intent_done(
        self, *, tenant_id: str, user_id: str, batch_id: str, intent: Intent
    ) -> bool:
        """Add `intent` to an awaiting batch's finished intents in one conditional UPDATE.
        True only for the caller that added it; a repeat or another owner's batch is False."""
        async with self._sessions() as session:
            result = await session.execute(
                text(
                    "UPDATE media_batches"
                    " SET completed_intents = json_insert(completed_intents, '$[#]', :intent)"
                    " WHERE id = :batch_id AND tenant_id = :tenant_id AND user_id = :user_id"
                    " AND status = :awaiting"
                    " AND NOT EXISTS ("
                    "   SELECT 1 FROM json_each(media_batches.completed_intents)"
                    "   WHERE json_each.value = :intent)"
                ),
                {
                    "intent": intent.value,
                    "batch_id": batch_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "awaiting": BatchStatus.AWAITING_INTENT.value,
                },
            )
            await session.commit()
            return result.rowcount == 1  # type: ignore[attr-defined,no-any-return]

    async def save_gate_result(
        self,
        *,
        tenant_id: str,
        user_id: str,
        batch_id: str,
        verdicts: Sequence[AssetVerdict],
    ) -> None:
        async with self._sessions() as session:
            for verdict in verdicts:
                await session.execute(
                    update(MediaAsset)
                    .where(
                        MediaAsset.id == verdict.asset_id,
                        MediaAsset.tenant_id == tenant_id,
                        MediaAsset.user_id == user_id,
                        MediaAsset.batch_id == batch_id,
                    )
                    .values(
                        phash=verdict.phash,
                        blur_score=verdict.blur_score,
                        is_blurry=verdict.is_blurry,
                        is_kept=verdict.is_kept,
                    )
                )
            await session.execute(
                update(MediaBatch)
                .where(
                    MediaBatch.id == batch_id,
                    MediaBatch.tenant_id == tenant_id,
                    MediaBatch.user_id == user_id,
                )
                .values(kept_count=sum(v.is_kept for v in verdicts))
            )
            await session.commit()

    async def get_batch(
        self, *, tenant_id: str, user_id: str, batch_id: str
    ) -> MediaBatchView | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(MediaBatch).where(
                        MediaBatch.id == batch_id,
                        MediaBatch.tenant_id == tenant_id,
                        MediaBatch.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
        return None if row is None else _view(row)

    async def batch_asset_ids(self, *, tenant_id: str, user_id: str, batch_id: str) -> list[str]:
        return await self._asset_ids(tenant_id, user_id, batch_id, kept_only=False)

    async def kept_asset_ids(self, *, tenant_id: str, user_id: str, batch_id: str) -> list[str]:
        """Kept assets, sharpest first."""
        return await self._asset_ids(tenant_id, user_id, batch_id, kept_only=True)

    async def _asset_ids(
        self, tenant_id: str, user_id: str, batch_id: str, *, kept_only: bool
    ) -> list[str]:
        query = select(MediaAsset.id).where(
            MediaAsset.batch_id == batch_id,
            MediaAsset.tenant_id == tenant_id,
            MediaAsset.user_id == user_id,
        )
        if kept_only:
            query = query.where(MediaAsset.is_kept.is_(True))
        query = query.order_by(func.coalesce(MediaAsset.blur_score, 0).desc(), MediaAsset.id)
        async with self._sessions() as session:
            return list((await session.execute(query)).scalars().all())

    async def list_orphan_batches(self) -> list[MediaBatchView]:
        """Startup maintenance only: batches a restart left in collecting or filtered."""
        async with self._sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(MediaBatch)
                        .where(
                            MediaBatch.status.in_(
                                (BatchStatus.COLLECTING.value, BatchStatus.FILTERED.value)
                            )
                        )
                        .order_by(MediaBatch.created_at, MediaBatch.id)
                    )
                )
                .scalars()
                .all()
            )
        return [_view(row) for row in rows]
