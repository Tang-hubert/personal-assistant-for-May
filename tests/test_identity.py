import pytest
from sqlalchemy.exc import IntegrityError

from src.adapters.storage.repository import SqlStorage
from src.domain.models import NewKnowledgeItem, ResolvedIdentity


async def test_channel_identity_is_unique(storage: SqlStorage) -> None:
    tenant_id = await storage.create_tenant(name="May")
    user_a = await storage.create_user(tenant_id=tenant_id, display_name="May")
    user_b = await storage.create_user(tenant_id=tenant_id, display_name="Other")
    await storage.link_channel_identity(
        tenant_id=tenant_id, user_id=user_a, channel="telegram", external_user_id="111"
    )
    with pytest.raises(IntegrityError):
        await storage.link_channel_identity(
            tenant_id=tenant_id, user_id=user_b, channel="telegram", external_user_id="111"
        )


async def test_unknown_identity_resolves_to_none(storage: SqlStorage) -> None:
    assert await storage.resolve_identity("telegram", "999") is None


async def test_one_person_on_two_channels_sees_same_memory(storage: SqlStorage) -> None:
    tenant_id = await storage.create_tenant(name="May")
    user_id = await storage.create_user(tenant_id=tenant_id, display_name="May")
    await storage.link_channel_identity(
        tenant_id=tenant_id, user_id=user_id, channel="telegram", external_user_id="111"
    )
    await storage.link_channel_identity(
        tenant_id=tenant_id, user_id=user_id, channel="line", external_user_id="U-line-1"
    )

    via_telegram = await storage.resolve_identity("telegram", "111")
    via_line = await storage.resolve_identity("line", "U-line-1")
    assert via_telegram == via_line == ResolvedIdentity(tenant_id=tenant_id, user_id=user_id)
    assert via_telegram is not None and via_line is not None

    item = NewKnowledgeItem(category="item_location", title="翡翠", content_text="翡翠放紅櫃子")
    item_id = await storage.save_knowledge(
        tenant_id=via_telegram.tenant_id, user_id=via_telegram.user_id, item=item
    )
    found = await storage.search_knowledge(
        tenant_id=via_line.tenant_id, user_id=via_line.user_id, query="翡翠"
    )
    assert [hit.id for hit in found] == [item_id]
