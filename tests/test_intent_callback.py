from datetime import timedelta

import pytest

from src.adapters.storage.media_repository import SqlMediaStore
from src.domain.media import (
    BatchStatus,
    Intent,
    IntentSelected,
    already_done,
    intent_already_done,
    resend_prompt,
)
from src.domain.models import IncomingEvent, ReplyTarget
from src.pipeline.batch_aggregator import BatchAggregator
from tests import imagegen
from tests.batch_helpers import Rig, batch_id_of
from tests.fakes import FakeChannel, FrozenClock, Owner


@pytest.fixture
def rig(media_store: SqlMediaStore, clock: FrozenClock, owner: Owner) -> Rig:
    return Rig(media_store, FakeChannel(), clock, owner)


async def _awaiting_batch(rig: Rig) -> tuple[BatchAggregator, str, list[str]]:
    aggregator = rig.aggregator()
    ids = [await rig.save(imagegen.jpeg(imagegen.sharp(1000 + i))) for i in range(2)]
    for n, asset in enumerate(ids):
        await aggregator.add(rig.image_event(asset, n=n))
    await aggregator.drain()
    return aggregator, batch_id_of(rig.channel.sent[0][1]), ids


async def _status(rig: Rig, batch_id: str) -> BatchStatus:
    batch = await rig.store.get_batch(
        tenant_id=rig.owner.tenant_id, user_id=rig.owner.user_id, batch_id=batch_id
    )
    assert batch is not None
    return batch.status


async def test_valid_callback_inside_ttl_selects_the_intent_and_kept_assets(rig: Rig) -> None:
    aggregator, batch_id, ids = await _awaiting_batch(rig)
    result = await aggregator.handle_intent_callback(rig.callback_event(batch_id, Intent.SALES))
    assert isinstance(result, IntentSelected)
    assert result.batch_id == batch_id
    assert result.intent is Intent.SALES
    assert set(result.kept_asset_ids) == set(ids)


def _scope(rig: Rig, batch_id: str) -> dict[str, str]:
    return {"tenant_id": rig.owner.tenant_id, "user_id": rig.owner.user_id, "batch_id": batch_id}


async def test_a_finished_intent_leaves_the_batch_open_for_another_intent(rig: Rig) -> None:
    aggregator, batch_id, ids = await _awaiting_batch(rig)
    assert await aggregator.complete(**_scope(rig, batch_id), intent=Intent.SALES)

    other = await aggregator.handle_intent_callback(rig.callback_event(batch_id, Intent.IOS))

    assert isinstance(other, IntentSelected) and other.intent is Intent.IOS
    assert set(other.kept_asset_ids) == set(ids)
    assert await _status(rig, batch_id) is BatchStatus.AWAITING_INTENT


async def test_tapping_a_finished_intent_again_is_a_stable_no_op(rig: Rig) -> None:
    aggregator, batch_id, _ = await _awaiting_batch(rig)
    assert await aggregator.complete(**_scope(rig, batch_id), intent=Intent.SALES)

    first = await aggregator.handle_intent_callback(rig.callback_event(batch_id, Intent.SALES))
    second = await aggregator.handle_intent_callback(rig.callback_event(batch_id, Intent.SALES))
    assert first == second == intent_already_done()


async def test_complete_twice_for_one_intent_reports_the_second_as_a_no_op(rig: Rig) -> None:
    aggregator, batch_id, _ = await _awaiting_batch(rig)
    scope = _scope(rig, batch_id)
    assert await aggregator.complete(**scope, intent=Intent.SALES) is True
    assert await aggregator.complete(**scope, intent=Intent.SALES) is False
    assert await aggregator.complete(**scope, intent=Intent.MEMORY) is True


async def test_after_the_ttl_a_finished_intent_stays_done_and_a_new_one_asks_to_resend(
    rig: Rig,
) -> None:
    aggregator, batch_id, _ = await _awaiting_batch(rig)
    assert await aggregator.complete(**_scope(rig, batch_id), intent=Intent.SALES)
    rig.clock.instant += timedelta(minutes=15)

    done = await aggregator.handle_intent_callback(rig.callback_event(batch_id, Intent.SALES))
    fresh = await aggregator.handle_intent_callback(rig.callback_event(batch_id, Intent.IOS))
    assert done == intent_already_done()
    assert fresh == resend_prompt()


async def test_a_batch_finished_before_t5_1_still_answers_already_done(rig: Rig) -> None:
    aggregator, batch_id, _ = await _awaiting_batch(rig)
    assert await rig.store.transition(
        **_scope(rig, batch_id), frm=BatchStatus.AWAITING_INTENT, to=BatchStatus.DONE
    )
    tapped = await aggregator.handle_intent_callback(rig.callback_event(batch_id, Intent.IOS))
    assert tapped == already_done()


async def test_another_owner_cannot_mark_an_intent_done(rig: Rig, other_owner: Owner) -> None:
    _, batch_id, _ = await _awaiting_batch(rig)
    assert not await rig.store.mark_intent_done(
        tenant_id=other_owner.tenant_id,
        user_id=other_owner.user_id,
        batch_id=batch_id,
        intent=Intent.SALES,
    )
    batch = await rig.store.get_batch(**_scope(rig, batch_id))
    assert batch is not None and batch.completed_intents == ()


async def test_callback_is_accepted_at_14_59_and_refused_at_15_00(rig: Rig) -> None:
    aggregator, batch_id, _ = await _awaiting_batch(rig)
    start = rig.clock.now()

    rig.clock.instant = start + timedelta(minutes=14, seconds=59)
    assert isinstance(
        await aggregator.handle_intent_callback(rig.callback_event(batch_id)), IntentSelected
    )

    rig.clock.instant = start + timedelta(minutes=15)
    assert await aggregator.handle_intent_callback(rig.callback_event(batch_id)) == resend_prompt()
    assert await _status(rig, batch_id) is BatchStatus.AWAITING_INTENT


async def test_another_tenants_batch_id_is_treated_as_not_found(
    rig: Rig, other_owner: Owner
) -> None:
    aggregator, batch_id, _ = await _awaiting_batch(rig)
    stranger = rig.callback_event(batch_id, owner=other_owner)
    assert await aggregator.handle_intent_callback(stranger) == resend_prompt()
    assert await _status(rig, batch_id) is BatchStatus.AWAITING_INTENT


@pytest.mark.parametrize("junk", ["garbage", "b:only-two", "b:x:not-an-intent", "x:1:lecture"])
async def test_malformed_callback_data_gets_the_resend_prompt(rig: Rig, junk: str) -> None:
    aggregator = rig.aggregator()
    event = IncomingEvent(
        event_id="cb-junk",
        tenant_id=rig.owner.tenant_id,
        user_id=rig.owner.user_id,
        channel="telegram",
        event_type="action_callback",
        reply_target=ReplyTarget(address="chat-1"),
        callback_data=junk,
    )
    assert await aggregator.handle_intent_callback(event) == resend_prompt()
