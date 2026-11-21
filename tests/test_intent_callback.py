from datetime import timedelta

import pytest

from src.adapters.storage.media_repository import SqlMediaStore
from src.domain.media import BatchStatus, Intent, IntentSelected, already_done, resend_prompt
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


async def test_repeated_callback_on_a_done_batch_is_a_stable_no_op(rig: Rig) -> None:
    aggregator, batch_id, _ = await _awaiting_batch(rig)
    assert await aggregator.complete(
        tenant_id=rig.owner.tenant_id, user_id=rig.owner.user_id, batch_id=batch_id
    )
    first = await aggregator.handle_intent_callback(rig.callback_event(batch_id))
    second = await aggregator.handle_intent_callback(rig.callback_event(batch_id))
    assert first == second == already_done()
    assert await _status(rig, batch_id) is BatchStatus.DONE


async def test_complete_twice_reports_the_second_as_a_no_op(rig: Rig) -> None:
    aggregator, batch_id, _ = await _awaiting_batch(rig)
    scope = {"tenant_id": rig.owner.tenant_id, "user_id": rig.owner.user_id, "batch_id": batch_id}
    assert await aggregator.complete(**scope) is True
    assert await aggregator.complete(**scope) is False


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
