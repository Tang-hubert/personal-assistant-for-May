import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from telegram import Chat, Update

from src.adapters.storage.media_repository import SqlMediaStore
from src.adapters.storage.repository import SqlStorage
from src.adapters.telegram.adapter import MAX_DOWNLOAD_BYTES, TelegramAdapter, TelegramChannel
from src.domain import replies
from src.domain.models import BotResponse, IncomingEvent, ResolvedIdentity
from tests.fakes import FakeNotifier, FrozenClock, Owner
from tests.telegram_helpers import (
    MAY_ID,
    STRANGER_ID,
    FakeBotApi,
    callback_update,
    document_update,
    photo_update,
    sticker_update,
    text_update,
    video_update,
    voice_update,
)

REPLY = BotResponse(title="好的")


@dataclass
class FakeRouter:
    events: list[IncomingEvent] = field(default_factory=list)
    error: Exception | None = None
    delay_s: float = 0.0

    async def handle(self, event: IncomingEvent) -> BotResponse | None:
        self.events.append(event)
        await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        return None if event.event_type == "image" else REPLY


class CountingStorage:
    def __init__(self, inner: SqlStorage) -> None:
        self.inner = inner
        self.lookups: list[str] = []

    async def resolve_identity(
        self, channel: str, external_user_id: str
    ) -> ResolvedIdentity | None:
        self.lookups.append(external_user_id)
        return await self.inner.resolve_identity(channel, external_user_id)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)


@dataclass
class Edge:
    adapter: TelegramAdapter
    bot: FakeBotApi
    router: FakeRouter
    notifier: FakeNotifier
    storage: CountingStorage

    async def feed(self, update: Update) -> None:
        await self.adapter.handle_update(update)


@pytest.fixture
async def edge(
    storage: SqlStorage, media_store: SqlMediaStore, owner: Owner, clock: FrozenClock
) -> Edge:
    await storage.link_channel_identity(
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        channel="telegram",
        external_user_id=str(MAY_ID),
    )
    bot, router, notifier = FakeBotApi(), FakeRouter(), FakeNotifier()
    counting = CountingStorage(storage)
    adapter = TelegramAdapter(
        bot=bot,
        channel=TelegramChannel(bot, counting, media_store),  # type: ignore[arg-type]
        router=router,
        storage=counting,  # type: ignore[arg-type]
        media=media_store,
        notifier=notifier,
        allowed_user_ids=frozenset({MAY_ID}),
        clock=clock,
        typing_interval_s=0.01,
    )
    return Edge(adapter, bot, router, notifier, counting)


async def test_a_stranger_gets_no_reply_no_download_and_no_lookup(
    edge: Edge, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    for update in (
        text_update("我的密碼是 1234", user_id=STRANGER_ID),
        photo_update(user_id=STRANGER_ID),
        callback_update(user_id=STRANGER_ID),
    ):
        await edge.feed(update)

    assert edge.bot.sent == edge.bot.downloads == edge.bot.answered == []
    assert edge.storage.lookups == [] and edge.router.events == []
    assert str(STRANGER_ID) in caplog.text
    assert "1234" not in caplog.text


async def test_a_group_message_is_ignored(edge: Edge) -> None:
    await edge.feed(text_update(chat_type=Chat.GROUP))
    assert edge.bot.sent == [] and edge.router.events == []


async def test_the_same_update_delivered_twice_is_handled_once(edge: Edge) -> None:
    await edge.feed(text_update(update_id=42))
    await edge.feed(text_update(update_id=42))

    assert [event.event_id for event in edge.router.events] == ["tg:u:42"]
    assert len(edge.bot.sent) == 1


async def test_a_callback_is_answered_and_keyed_by_its_query_id(edge: Edge) -> None:
    update = callback_update("s:finish")
    assert update.callback_query is not None

    await edge.feed(update)

    assert edge.bot.answered == [update.callback_query.id]
    (event,) = edge.router.events
    assert event.event_id == f"tg:cb:{update.callback_query.id}"
    assert event.event_type == "action_callback" and event.callback_data == "s:finish"
    assert [sent.chat_id for sent in edge.bot.sent] == [MAY_ID]


async def test_a_callback_on_a_message_too_old_to_include_replies_to_the_user(
    edge: Edge,
) -> None:
    await edge.feed(callback_update(with_message=False))

    assert edge.router.events[0].reply_target.address == str(MAY_ID)
    assert [sent.chat_id for sent in edge.bot.sent] == [MAY_ID]


async def test_a_photo_downloads_the_largest_size_without_a_reply(
    edge: Edge, media_store: SqlMediaStore, owner: Owner
) -> None:
    await edge.feed(photo_update())

    assert edge.bot.downloads == ["large"]
    (event,) = edge.router.events
    assert event.event_type == "image"
    blob = await media_store.read_asset_blob(
        tenant_id=owner.tenant_id, user_id=owner.user_id, asset_id=event.asset_ids[0]
    )
    assert blob is not None and blob.mime_type == "image/jpeg"
    assert edge.bot.sent == [] and edge.bot.chat_actions == []


@pytest.mark.parametrize(
    ("mime_type", "file_name", "mime"),
    [
        ("image/heic", "IMG_0001.HEIC", "image/heic"),
        (None, "IMG_0002.heif", "image/heif"),
        ("application/octet-stream", "IMG_0003.HEIC", "image/heic"),
        ("image/png", "screen.png", "image/png"),
    ],
)
async def test_an_image_document_becomes_an_image_event(
    edge: Edge,
    media_store: SqlMediaStore,
    owner: Owner,
    mime_type: str | None,
    file_name: str,
    mime: str,
) -> None:
    await edge.feed(document_update(mime_type, file_name))

    (event,) = edge.router.events
    assert event.event_type == "image"
    blob = await media_store.read_asset_blob(
        tenant_id=owner.tenant_id, user_id=owner.user_id, asset_id=event.asset_ids[0]
    )
    assert blob is not None and blob.mime_type == mime


@pytest.mark.parametrize(
    "update",
    [
        document_update("application/pdf", "report.pdf"),
        sticker_update(),
        video_update(),
    ],
    ids=["pdf", "sticker", "video"],
)
async def test_other_content_is_an_unsupported_event(edge: Edge, update: Update) -> None:
    await edge.feed(update)

    (event,) = edge.router.events
    assert event.event_type == "unsupported"
    assert edge.bot.downloads == []


async def test_a_121_second_voice_is_refused_before_download(edge: Edge) -> None:
    await edge.feed(voice_update(duration=121))

    assert edge.bot.downloads == [] and edge.router.events == []
    assert len(edge.bot.sent) == 1 and replies.voice_too_long().title in edge.bot.sent[0].text


async def test_a_zero_second_voice_is_unclear_before_download(edge: Edge) -> None:
    await edge.feed(voice_update(duration=0))

    assert edge.bot.downloads == [] and edge.router.events == []
    assert replies.voice_unclear().title in edge.bot.sent[0].text


async def test_a_30_second_voice_is_downloaded_as_ogg_with_its_duration(
    edge: Edge, media_store: SqlMediaStore, owner: Owner
) -> None:
    await edge.feed(voice_update(duration=30))

    assert edge.bot.downloads == ["voice"]
    (event,) = edge.router.events
    assert event.event_type == "audio" and event.audio_duration_s == 30.0
    blob = await media_store.read_asset_blob(
        tenant_id=owner.tenant_id, user_id=owner.user_id, asset_id=event.asset_ids[0]
    )
    assert blob is not None and blob.mime_type == "audio/ogg"


@pytest.mark.parametrize(
    "make",
    [
        lambda size: photo_update(file_size=size),
        lambda size: document_update("image/heic", file_size=size),
        lambda size: voice_update(file_size=size),
    ],
    ids=["photo", "document", "voice"],
)
async def test_a_file_over_15_mib_is_refused_before_download(edge: Edge, make: object) -> None:
    await edge.feed(make(MAX_DOWNLOAD_BYTES + 1))  # type: ignore[operator]

    assert edge.bot.downloads == [] and edge.router.events == []
    assert len(edge.bot.sent) == 1
    assert replies.file_too_large().title in edge.bot.sent[0].text


@pytest.mark.parametrize(
    "make",
    [
        lambda size: photo_update(file_size=size),
        lambda size: document_update("image/heic", file_size=size),
        lambda size: voice_update(file_size=size),
    ],
    ids=["photo", "document", "voice"],
)
@pytest.mark.parametrize("size", [MAX_DOWNLOAD_BYTES, None], ids=["exactly-15-mib", "unknown"])
async def test_a_file_of_15_mib_or_unknown_size_still_downloads(
    edge: Edge, make: object, size: int | None
) -> None:
    await edge.feed(make(size))  # type: ignore[operator]

    assert len(edge.bot.downloads) == 1
    assert len(edge.router.events) == 1


async def test_text_shows_typing_while_waiting_and_sends_one_reply(edge: Edge) -> None:
    edge.router.delay_s = 0.05

    await edge.feed(text_update("翡翠放哪"))

    (event,) = edge.router.events
    assert event.payload_text == "翡翠放哪"
    assert event.reply_target.address == str(MAY_ID)
    assert len(edge.bot.chat_actions) >= 2
    assert set(edge.bot.chat_actions) == {(MAY_ID, "typing")}
    assert len(edge.bot.sent) == 1 and "好的" in edge.bot.sent[0].text


async def test_typing_stops_once_the_reply_is_sent(edge: Edge) -> None:
    await edge.feed(text_update())
    actions = len(edge.bot.chat_actions)
    await asyncio.sleep(0.05)
    assert len(edge.bot.chat_actions) == actions


async def test_a_router_failure_becomes_one_busy_reply_and_an_ops_alert(
    edge: Edge, caplog: pytest.LogCaptureFixture
) -> None:
    edge.router.error = KeyError("私人內容 翡翠")

    await edge.feed(text_update("翡翠放哪"))

    assert len(edge.bot.sent) == 1
    assert replies.busy_message().title in edge.bot.sent[0].text
    assert edge.notifier.sent == [("error", "telegram update failed: KeyError")]
    assert "KeyError" in caplog.text
    assert "私人內容" not in caplog.text


async def test_a_failed_send_is_not_retried_and_alerts_ops(edge: Edge) -> None:
    edge.bot.fail_send = True
    await edge.feed(text_update())
    assert edge.bot.sent == []
    assert edge.notifier.sent == [("error", "telegram reply not sent: RuntimeError")]


async def test_a_burst_whose_downloads_fail_gets_one_notice(edge: Edge) -> None:
    edge.bot.fail_download = True

    for n in range(4):
        await edge.feed(photo_update(file_id=f"p{n}", media_group_id="album"))

    (sent,) = edge.bot.sent
    assert replies.photo_not_received().title in sent.text
    assert edge.router.events == []
    assert edge.notifier.sent == [("error", "telegram photo not received: RuntimeError")]


async def test_a_burst_of_oversized_photos_gets_one_notice(edge: Edge) -> None:
    for n in range(4):
        await edge.feed(photo_update(file_id=f"p{n}", file_size=MAX_DOWNLOAD_BYTES + 1))

    (sent,) = edge.bot.sent
    assert replies.file_too_large().title in sent.text
    assert edge.bot.downloads == []


async def test_a_photo_notice_comes_again_only_after_a_minute(
    edge: Edge, clock: FrozenClock
) -> None:
    edge.bot.fail_download = True

    await edge.feed(photo_update(file_id="a"))
    clock.instant += timedelta(seconds=59)
    await edge.feed(photo_update(file_id="b", file_size=MAX_DOWNLOAD_BYTES + 1))
    assert len(edge.bot.sent) == 1
    clock.instant += timedelta(seconds=1)
    await edge.feed(photo_update(file_id="c"))

    assert len(edge.bot.sent) == 2


@pytest.mark.parametrize(
    "make",
    [
        lambda: text_update(),
        lambda: photo_update(),
        lambda: document_update("image/heic"),
        lambda: document_update("application/pdf", "a.pdf"),
        lambda: voice_update(30),
        lambda: voice_update(121),
        lambda: voice_update(file_size=MAX_DOWNLOAD_BYTES + 1),
        lambda: sticker_update(),
        lambda: callback_update(),
        lambda: callback_update(with_message=False),
    ],
)
@pytest.mark.parametrize("fails", [False, True], ids=["ok", "router-fails"])
async def test_every_event_sends_at_most_one_message(edge: Edge, make: object, fails: bool) -> None:
    if fails:
        edge.router.error = RuntimeError("boom")
    await edge.feed(make())  # type: ignore[operator]
    assert len(edge.bot.sent) <= 1
