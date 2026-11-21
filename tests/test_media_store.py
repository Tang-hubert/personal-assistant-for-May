import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest

from src.adapters.storage.media_repository import SqlMediaStore
from src.domain.media import BatchStatus
from tests.fakes import FrozenClock, Owner


async def _open(store: SqlMediaStore, owner: Owner) -> str:
    return await store.open_batch(
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        channel="telegram",
        media_group_id=None,
        reply_address="chat-1",
    )


async def _asset(store: SqlMediaStore, owner: Owner, data: bytes = b"x") -> str:
    return await store.save_asset(
        tenant_id=owner.tenant_id, user_id=owner.user_id, data=data, media_type="image", ext="jpg"
    )


async def test_asset_is_written_under_data_dir_with_relative_posix_path(
    media_store: SqlMediaStore, owner: Owner, tmp_path: Path, db_path: Path
) -> None:
    asset_id = await _asset(media_store, owner, b"jpegbytes")
    stored = tmp_path / "data" / "media" / owner.tenant_id / f"{asset_id}.jpg"
    assert stored.read_bytes() == b"jpegbytes"
    assert (
        await media_store.read_asset(
            tenant_id=owner.tenant_id, user_id=owner.user_id, asset_id=asset_id
        )
        == b"jpegbytes"
    )
    with closing(sqlite3.connect(db_path)) as conn:
        (local_path,) = conn.execute(
            "SELECT local_path FROM media_assets WHERE id = ?", (asset_id,)
        ).fetchone()
    assert local_path == f"media/{owner.tenant_id}/{asset_id}.jpg"


async def test_other_user_or_tenant_cannot_read_an_asset(
    media_store: SqlMediaStore, owner: Owner, other_owner: Owner
) -> None:
    asset_id = await _asset(media_store, owner)
    assert (
        await media_store.read_asset(
            tenant_id=other_owner.tenant_id, user_id=other_owner.user_id, asset_id=asset_id
        )
        is None
    )
    assert (
        await media_store.read_asset(
            tenant_id=owner.tenant_id, user_id=other_owner.user_id, asset_id=asset_id
        )
        is None
    )


async def test_unsafe_extension_is_rejected(media_store: SqlMediaStore, owner: Owner) -> None:
    with pytest.raises(ValueError, match="extension"):
        await media_store.save_asset(
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            data=b"x",
            media_type="image",
            ext="../x",
        )


async def test_transition_is_conditional_and_idempotent(
    media_store: SqlMediaStore, owner: Owner
) -> None:
    batch_id = await _open(media_store, owner)
    scope = {"tenant_id": owner.tenant_id, "user_id": owner.user_id, "batch_id": batch_id}
    first = await media_store.transition(
        **scope, frm=BatchStatus.COLLECTING, to=BatchStatus.FILTERED
    )
    second = await media_store.transition(
        **scope, frm=BatchStatus.COLLECTING, to=BatchStatus.FILTERED
    )
    assert (first, second) == (True, False)
    batch = await media_store.get_batch(**scope)
    assert batch is not None and batch.status is BatchStatus.FILTERED


async def test_illegal_transition_raises_and_changes_nothing(
    media_store: SqlMediaStore, owner: Owner
) -> None:
    batch_id = await _open(media_store, owner)
    scope = {"tenant_id": owner.tenant_id, "user_id": owner.user_id, "batch_id": batch_id}
    with pytest.raises(ValueError, match="illegal"):
        await media_store.transition(**scope, frm=BatchStatus.DONE, to=BatchStatus.COLLECTING)
    with pytest.raises(ValueError, match="illegal"):
        await media_store.transition(**scope, frm=BatchStatus.COLLECTING, to=BatchStatus.DONE)
    batch = await media_store.get_batch(**scope)
    assert batch is not None and batch.status is BatchStatus.COLLECTING


async def test_awaiting_intent_stores_its_expiry(
    media_store: SqlMediaStore, owner: Owner, clock: FrozenClock
) -> None:
    batch_id = await _open(media_store, owner)
    scope = {"tenant_id": owner.tenant_id, "user_id": owner.user_id, "batch_id": batch_id}
    expires = clock.now() + timedelta(minutes=15)
    await media_store.transition(**scope, frm=BatchStatus.COLLECTING, to=BatchStatus.FILTERED)
    await media_store.transition(
        **scope,
        frm=BatchStatus.FILTERED,
        to=BatchStatus.AWAITING_INTENT,
        intent_expires_at=expires,
    )
    batch = await media_store.get_batch(**scope)
    assert batch is not None and batch.intent_expires_at == expires


async def test_attach_counts_each_asset_once_and_only_for_the_owner(
    media_store: SqlMediaStore, owner: Owner, other_owner: Owner
) -> None:
    batch_id = await _open(media_store, owner)
    mine = await _asset(media_store, owner, b"1")
    theirs = await _asset(media_store, other_owner, b"2")
    scope = {"tenant_id": owner.tenant_id, "user_id": owner.user_id, "batch_id": batch_id}
    await media_store.attach_assets(**scope, asset_ids=[mine, theirs])
    await media_store.attach_assets(**scope, asset_ids=[mine])
    batch = await media_store.get_batch(**scope)
    assert batch is not None and batch.raw_count == 1
    assert await media_store.batch_asset_ids(**scope) == [mine]


async def test_get_batch_is_scoped_to_its_owner(
    media_store: SqlMediaStore, owner: Owner, other_owner: Owner
) -> None:
    batch_id = await _open(media_store, owner)
    assert (
        await media_store.get_batch(
            tenant_id=other_owner.tenant_id, user_id=other_owner.user_id, batch_id=batch_id
        )
        is None
    )


async def test_orphan_listing_covers_only_collecting_and_filtered_across_tenants(
    media_store: SqlMediaStore, owner: Owner, other_owner: Owner
) -> None:
    collecting = await _open(media_store, owner)
    filtered = await _open(media_store, other_owner)
    finished = await _open(media_store, owner)
    await media_store.transition(
        tenant_id=other_owner.tenant_id,
        user_id=other_owner.user_id,
        batch_id=filtered,
        frm=BatchStatus.COLLECTING,
        to=BatchStatus.FILTERED,
    )
    for frm, to in (
        (BatchStatus.COLLECTING, BatchStatus.FILTERED),
        (BatchStatus.FILTERED, BatchStatus.DONE),
    ):
        await media_store.transition(
            tenant_id=owner.tenant_id, user_id=owner.user_id, batch_id=finished, frm=frm, to=to
        )
    orphans = await media_store.list_orphan_batches()
    assert {b.id for b in orphans} == {collecting, filtered}


@pytest.mark.parametrize(
    ("media_type", "ext", "mime"),
    [("image", "jpg", "image/jpeg"), ("image", "PNG", "image/png"), ("audio", "ogg", "audio/ogg")],
)
async def test_asset_blob_carries_the_mime_type_of_its_extension(
    media_store: SqlMediaStore, owner: Owner, media_type: str, ext: str, mime: str
) -> None:
    asset_id = await media_store.save_asset(
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        data=b"bytes",
        media_type=media_type,  # type: ignore[arg-type]
        ext=ext,
    )
    blob = await media_store.read_asset_blob(
        tenant_id=owner.tenant_id, user_id=owner.user_id, asset_id=asset_id
    )
    assert blob is not None
    assert (blob.data, blob.mime_type) == (b"bytes", mime)


async def test_asset_blob_is_owner_scoped(
    media_store: SqlMediaStore, owner: Owner, other_owner: Owner
) -> None:
    asset_id = await _asset(media_store, owner)
    assert (
        await media_store.read_asset_blob(
            tenant_id=other_owner.tenant_id, user_id=other_owner.user_id, asset_id=asset_id
        )
        is None
    )
