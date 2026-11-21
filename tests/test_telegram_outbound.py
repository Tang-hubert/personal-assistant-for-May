import pytest

from src.adapters.storage.media_repository import SqlMediaStore
from src.adapters.storage.repository import SqlStorage
from src.adapters.telegram.adapter import TelegramChannel
from src.adapters.telegram.render import CAPTION_MAX, utf16_len
from src.domain.models import BotResponse, ReplyTarget
from tests import imagegen
from tests.fakes import Owner
from tests.telegram_helpers import MAY_ID, FakeBotApi

TARGET = ReplyTarget(address=str(MAY_ID))


@pytest.fixture
async def linked(storage: SqlStorage, owner: Owner) -> Owner:
    await storage.link_channel_identity(
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        channel="telegram",
        external_user_id=str(MAY_ID),
    )
    return owner


async def _asset(media: SqlMediaStore, owner: Owner, data: bytes, ext: str) -> str:
    return await media.save_asset(
        tenant_id=owner.tenant_id, user_id=owner.user_id, data=data, media_type="image", ext=ext
    )


async def test_a_reply_with_a_jpeg_asset_is_one_photo_with_a_caption(
    storage: SqlStorage, media_store: SqlMediaStore, linked: Owner
) -> None:
    data = imagegen.jpeg(imagegen.sharp(1))
    asset = await _asset(media_store, linked, data, "jpg")
    bot = FakeBotApi()

    await TelegramChannel(bot, storage, media_store).send(
        TARGET, BotResponse(title="找到了", bullets=("💎" * 900,), image_asset_id=asset)
    )

    (sent,) = bot.sent
    assert sent.kind == "photo" and sent.photo == data and sent.chat_id == MAY_ID
    assert utf16_len(sent.text) <= CAPTION_MAX


async def test_a_heic_asset_is_sent_as_a_jpeg(
    storage: SqlStorage, media_store: SqlMediaStore, linked: Owner
) -> None:
    asset = await _asset(media_store, linked, imagegen.heic(imagegen.sharp(2)), "heic")
    bot = FakeBotApi()

    await TelegramChannel(bot, storage, media_store).send(
        TARGET, BotResponse(title="找到了", image_asset_id=asset)
    )

    (sent,) = bot.sent
    assert sent.photo is not None and sent.photo[:3] == b"\xff\xd8\xff"


async def test_a_missing_asset_falls_back_to_one_text_message(
    storage: SqlStorage, media_store: SqlMediaStore, linked: Owner
) -> None:
    bot = FakeBotApi()

    await TelegramChannel(bot, storage, media_store).send(
        TARGET, BotResponse(title="找到了", image_asset_id="no-such-asset")
    )

    (sent,) = bot.sent
    assert sent.kind == "message" and "找到了" in sent.text


async def test_another_owners_asset_is_never_sent(
    storage: SqlStorage, media_store: SqlMediaStore, linked: Owner, other_owner: Owner
) -> None:
    asset = await _asset(media_store, other_owner, imagegen.jpeg(imagegen.sharp(3)), "jpg")
    bot = FakeBotApi()

    await TelegramChannel(bot, storage, media_store).send(
        TARGET, BotResponse(title="找到了", image_asset_id=asset)
    )

    (sent,) = bot.sent
    assert sent.kind == "message"


async def test_an_asset_that_cannot_be_converted_is_replied_as_text(
    storage: SqlStorage, media_store: SqlMediaStore, linked: Owner
) -> None:
    asset = await _asset(media_store, linked, b"not an image", "heic")
    bot = FakeBotApi()

    await TelegramChannel(bot, storage, media_store).send(
        TARGET, BotResponse(title="找到了", bullets=("翡翠在紅櫃子",), image_asset_id=asset)
    )

    (sent,) = bot.sent
    assert sent.kind == "message" and "翡翠在紅櫃子" in sent.text


async def test_a_rejected_photo_upload_is_replied_as_text(
    storage: SqlStorage, media_store: SqlMediaStore, linked: Owner
) -> None:
    asset = await _asset(media_store, linked, imagegen.jpeg(imagegen.sharp(4)), "jpg")
    bot = FakeBotApi(fail_photo=True)

    await TelegramChannel(bot, storage, media_store).send(
        TARGET, BotResponse(title="找到了", bullets=("翡翠在紅櫃子",), image_asset_id=asset)
    )

    (sent,) = bot.sent
    assert sent.kind == "message" and "翡翠在紅櫃子" in sent.text
