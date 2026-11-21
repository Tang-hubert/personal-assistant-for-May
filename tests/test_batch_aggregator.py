import sqlite3
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest

from src.adapters.storage.media_repository import SqlMediaStore
from src.domain.media import BatchStatus
from src.domain.models import BotResponse, ReplyTarget
from tests import imagegen
from tests.batch_helpers import Rig, batch_id_of
from tests.fakes import FakeChannel, FrozenClock, Owner


@pytest.fixture
def rig(media_store: SqlMediaStore, clock: FrozenClock, owner: Owner) -> Rig:
    return Rig(media_store, FakeChannel(), clock, owner)


async def _batch(rig: Rig, batch_id: str):  # type: ignore[no-untyped-def]
    batch = await rig.store.get_batch(
        tenant_id=rig.owner.tenant_id, user_id=rig.owner.user_id, batch_id=batch_id
    )
    assert batch is not None
    return batch


async def test_fifteen_photo_burst_becomes_one_batch_and_one_response(rig: Rig) -> None:
    originals = [imagegen.sharp(100 + i) for i in range(6)]
    photos: list[tuple[str, bytes]] = []
    for i, pixels in enumerate(originals):
        photos.append((f"orig{i}", imagegen.jpeg(pixels, 95)))
    for i in range(5):
        photos.append((f"dupe{i}", imagegen.jpeg(imagegen.dimmer(originals[i]), 50)))
    for i in range(4):
        photos.append((f"blur{i}", imagegen.jpeg(imagegen.blurry(200 + i))))

    ids = {label: await rig.save(data) for label, data in photos}
    aggregator = rig.aggregator()
    labels = list(ids)
    for n, label in enumerate(labels):
        group = "g1" if n < 7 else "g2" if n < 14 else None
        await aggregator.add(rig.image_event(ids[label], n=n, media_group_id=group))
    await aggregator.drain()

    assert len(rig.channel.sent) == 1
    target, response = rig.channel.sent[0]
    assert target.address == "chat-1"
    assert len(response.actions) == 3
    batch_id = batch_id_of(response)
    assert all(batch_id in a.callback_data for a in response.actions)

    batch = await _batch(rig, batch_id)
    assert batch.raw_count == 15
    assert batch.kept_count == 6
    assert batch.status is BatchStatus.AWAITING_INTENT
    assert batch.intent_expires_at == rig.clock.now() + timedelta(minutes=15)
    kept = await rig.store.kept_asset_ids(
        tenant_id=rig.owner.tenant_id, user_id=rig.owner.user_id, batch_id=batch_id
    )
    assert set(kept) == {ids[f"orig{i}"] for i in range(6)}
    assert {b.id for b in await rig.store.list_orphan_batches()} == set()


async def test_last_event_reply_target_is_kept_for_the_response(rig: Rig) -> None:
    aggregator = rig.aggregator()
    first = await rig.save(imagegen.jpeg(imagegen.sharp(300)))
    last = await rig.save(imagegen.jpeg(imagegen.sharp(301)))
    await aggregator.add(rig.image_event(first, token="old-token"))
    await aggregator.add(rig.image_event(last, token="fresh-token"))
    await aggregator.drain()
    ((target, _),) = rig.channel.sent
    assert target.address == "chat-1"
    assert target.ephemeral_token == "fresh-token"


async def test_line_group_closes_as_soon_as_the_last_image_arrives(rig: Rig) -> None:
    aggregator = rig.aggregator(debounce_s=10.0)
    started = time.monotonic()
    for index in (1, 2, 3):
        asset = await rig.save(imagegen.jpeg(imagegen.sharp(400 + index)))
        await aggregator.add(
            rig.image_event(asset, media_group_id="set-1", group_index=index, group_total=3)
        )
    await aggregator.drain()
    assert time.monotonic() - started < 1.0
    assert len(rig.channel.sent) == 1
    batch = await _batch(rig, batch_id_of(rig.channel.sent[0][1]))
    assert batch.raw_count == 3


async def test_bursts_further_apart_than_debounce_are_separate_batches(rig: Rig) -> None:
    aggregator = rig.aggregator()
    for seed in (500, 501):
        asset = await rig.save(imagegen.jpeg(imagegen.sharp(seed)))
        await aggregator.add(rig.image_event(asset, n=seed))
        await aggregator.drain()
    assert len(rig.channel.sent) == 2
    first, second = (batch_id_of(r) for _, r in rig.channel.sent)
    assert first != second


async def test_all_blurry_burst_finishes_with_a_retake_prompt_and_no_buttons(
    rig: Rig, db_path: Path
) -> None:
    aggregator = rig.aggregator()
    for i in range(3):
        asset = await rig.save(imagegen.jpeg(imagegen.blurry(600 + i)))
        await aggregator.add(rig.image_event(asset, n=i, media_group_id="g"))
    await aggregator.drain()
    ((_, response),) = rig.channel.sent
    assert response.actions == ()
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("SELECT status, raw_count, kept_count FROM media_batches").fetchall()
    assert rows == [("done", 3, 0)]


async def test_new_burst_while_awaiting_intent_opens_a_new_batch_and_old_buttons_live(
    rig: Rig,
) -> None:
    aggregator = rig.aggregator()
    first = await rig.save(imagegen.jpeg(imagegen.sharp(700)))
    await aggregator.add(rig.image_event(first))
    await aggregator.drain()
    second = await rig.save(imagegen.jpeg(imagegen.sharp(701)))
    await aggregator.add(rig.image_event(second))
    await aggregator.drain()

    old_id, new_id = (batch_id_of(r) for _, r in rig.channel.sent)
    assert old_id != new_id
    assert (await _batch(rig, old_id)).status is BatchStatus.AWAITING_INTENT
    result = await aggregator.handle_intent_callback(rig.callback_event(old_id))
    assert not hasattr(result, "title")


async def _orphan(rig: Rig, *, filtered: bool) -> str:
    batch_id = await rig.store.open_batch(
        tenant_id=rig.owner.tenant_id,
        user_id=rig.owner.user_id,
        channel="telegram",
        media_group_id=None,
        reply_address="chat-77",
    )
    ids = [await rig.save(imagegen.jpeg(imagegen.sharp(800 + i), 95)) for i in range(3)] + [
        await rig.save(imagegen.jpeg(imagegen.blurry(850)))
    ]
    scope = {"tenant_id": rig.owner.tenant_id, "user_id": rig.owner.user_id, "batch_id": batch_id}
    await rig.store.attach_assets(**scope, asset_ids=ids)
    if filtered:
        await rig.store.transition(**scope, frm=BatchStatus.COLLECTING, to=BatchStatus.FILTERED)
    return batch_id


@pytest.mark.parametrize("filtered", [False, True])
async def test_orphan_recovery_sends_once_and_is_idempotent(rig: Rig, filtered: bool) -> None:
    batch_id = await _orphan(rig, filtered=filtered)
    fresh_process = rig.aggregator()

    assert await fresh_process.recover_orphans() == 1
    assert len(rig.channel.sent) == 1
    target, response = rig.channel.sent[0]
    assert target.address == "chat-77"
    assert target.ephemeral_token is None
    assert batch_id_of(response) == batch_id
    batch = await _batch(rig, batch_id)
    assert batch.status is BatchStatus.AWAITING_INTENT
    assert batch.kept_count == 3

    assert await rig.aggregator().recover_orphans() == 0
    assert len(rig.channel.sent) == 1
    assert (await _batch(rig, batch_id)).status is BatchStatus.AWAITING_INTENT


async def test_recovery_leaves_awaiting_and_done_batches_alone(rig: Rig) -> None:
    aggregator = rig.aggregator()
    asset = await rig.save(imagegen.jpeg(imagegen.sharp(900)))
    await aggregator.add(rig.image_event(asset))
    await aggregator.drain()
    batch_id = batch_id_of(rig.channel.sent[0][1])
    await aggregator.complete(
        tenant_id=rig.owner.tenant_id, user_id=rig.owner.user_id, batch_id=batch_id
    )
    other = await rig.save(imagegen.jpeg(imagegen.sharp(901)))
    await aggregator.add(rig.image_event(other))
    await aggregator.drain()
    sent_before = len(rig.channel.sent)

    assert await rig.aggregator().recover_orphans() == 0
    assert len(rig.channel.sent) == sent_before
    assert (await _batch(rig, batch_id)).status is BatchStatus.DONE


async def test_failed_send_leaves_the_batch_recoverable(rig: Rig) -> None:
    batch_id = await _orphan(rig, filtered=False)
    healthy_send = rig.channel.send

    async def broken_send(target: ReplyTarget, response: BotResponse) -> None:
        raise ConnectionError("telegram unreachable")

    rig.channel.send = broken_send  # type: ignore[method-assign]
    assert await rig.aggregator().recover_orphans() == 0
    assert (await _batch(rig, batch_id)).status is BatchStatus.FILTERED

    rig.channel.send = healthy_send  # type: ignore[method-assign]
    assert await rig.aggregator().recover_orphans() == 1
    assert len(rig.channel.sent) == 1
    assert (await _batch(rig, batch_id)).status is BatchStatus.AWAITING_INTENT
