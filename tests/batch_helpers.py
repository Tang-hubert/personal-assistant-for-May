from dataclasses import dataclass

from src.adapters.storage.media_repository import SqlMediaStore
from src.domain.media import Intent, encode_callback, parse_callback
from src.domain.models import BotResponse, IncomingEvent, ReplyTarget
from src.pipeline.batch_aggregator import BatchAggregator
from tests.fakes import FakeChannel, FrozenClock, Owner

DEBOUNCE = 0.05
BLUR_THRESHOLD = 100.0


@dataclass
class Rig:
    store: SqlMediaStore
    channel: FakeChannel
    clock: FrozenClock
    owner: Owner

    def aggregator(self, *, debounce_s: float = DEBOUNCE) -> BatchAggregator:
        return BatchAggregator(
            self.store,
            self.channel,
            self.clock,
            blur_threshold=BLUR_THRESHOLD,
            debounce_s=debounce_s,
        )

    async def save(self, data: bytes, owner: Owner | None = None) -> str:
        who = owner or self.owner
        return await self.store.save_asset(
            tenant_id=who.tenant_id,
            user_id=who.user_id,
            data=data,
            media_type="image",
            ext="jpg",
        )

    def image_event(
        self,
        asset_id: str,
        *,
        n: int = 0,
        media_group_id: str | None = None,
        group_index: int | None = None,
        group_total: int | None = None,
        token: str | None = None,
        owner: Owner | None = None,
    ) -> IncomingEvent:
        who = owner or self.owner
        return IncomingEvent(
            event_id=f"e-{asset_id}-{n}",
            tenant_id=who.tenant_id,
            user_id=who.user_id,
            channel="telegram",
            event_type="image",
            reply_target=ReplyTarget(address="chat-1", ephemeral_token=token),
            asset_ids=(asset_id,),
            media_group_id=media_group_id,
            group_index=group_index,
            group_total=group_total,
        )

    def callback_event(
        self, batch_id: str, intent: Intent = Intent.LECTURE, owner: Owner | None = None
    ) -> IncomingEvent:
        who = owner or self.owner
        return IncomingEvent(
            event_id=f"cb-{batch_id}",
            tenant_id=who.tenant_id,
            user_id=who.user_id,
            channel="telegram",
            event_type="action_callback",
            reply_target=ReplyTarget(address="chat-1"),
            callback_data=encode_callback(batch_id, intent),
        )


def batch_id_of(response: BotResponse) -> str:
    parsed = parse_callback(response.actions[0].callback_data)
    assert parsed is not None
    return parsed[0]
