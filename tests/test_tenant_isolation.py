from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from src.adapters.storage.orm import MediaAsset, TopicSession
from src.adapters.storage.repository import SqlStorage
from src.domain.models import NewKnowledgeItem

NOW = datetime(2026, 11, 21, 3, 0, tzinfo=UTC)
ITEM = NewKnowledgeItem(category="item_location", title="翡翠", content_text="翡翠放紅櫃子")


async def _person(storage: SqlStorage, tenant_name: str) -> tuple[str, str]:
    tenant_id = await storage.create_tenant(name=tenant_name)
    user_id = await storage.create_user(tenant_id=tenant_id, display_name=tenant_name)
    return tenant_id, user_id


async def test_items_are_invisible_across_tenants(storage: SqlStorage) -> None:
    tenant_a, user_a = await _person(storage, "A")
    tenant_b, user_b = await _person(storage, "B")
    item_id = await storage.save_knowledge(tenant_id=tenant_a, user_id=user_a, item=ITEM)

    owned = await storage.get_knowledge(tenant_id=tenant_a, user_id=user_a, item_id=item_id)
    assert owned is not None and owned.content_text == "翡翠放紅櫃子"
    assert owned.created_at == NOW

    assert await storage.get_knowledge(tenant_id=tenant_b, user_id=user_b, item_id=item_id) is None
    assert await storage.get_knowledge(tenant_id=tenant_b, user_id=user_a, item_id=item_id) is None
    assert await storage.search_knowledge(tenant_id=tenant_b, user_id=user_b, query="翡翠") == []


async def test_items_are_invisible_to_another_person_in_same_tenant(storage: SqlStorage) -> None:
    tenant_id, user_a = await _person(storage, "家")
    user_b = await storage.create_user(tenant_id=tenant_id, display_name="家人")
    item_id = await storage.save_knowledge(tenant_id=tenant_id, user_id=user_a, item=ITEM)

    assert await storage.get_knowledge(tenant_id=tenant_id, user_id=user_b, item_id=item_id) is None
    assert await storage.search_knowledge(tenant_id=tenant_id, user_id=user_b, query="翡翠") == []


async def test_user_cannot_be_attached_to_a_foreign_tenant(storage: SqlStorage) -> None:
    _, user_a = await _person(storage, "A")
    tenant_b, _ = await _person(storage, "B")
    with pytest.raises(IntegrityError):
        await storage.save_knowledge(tenant_id=tenant_b, user_id=user_a, item=ITEM)


def _session(tenant_id: str, user_id: str, status: str) -> TopicSession:
    return TopicSession(
        tenant_id=tenant_id,
        user_id=user_id,
        topic_type="general",
        status=status,
        last_active_at=NOW,
        created_at=NOW,
    )


async def test_at_most_one_active_session_per_user(storage: SqlStorage) -> None:
    tenant_id, user_id = await _person(storage, "A")
    async with storage.session() as session:
        session.add_all(
            [_session(tenant_id, user_id, "closed"), _session(tenant_id, user_id, "closed")]
        )
        session.add(_session(tenant_id, user_id, "active"))
        await session.commit()

    async with storage.session() as session:
        session.add(_session(tenant_id, user_id, "active"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_extraction_count_cannot_exceed_one(storage: SqlStorage) -> None:
    tenant_id, user_id = await _person(storage, "A")
    row = _session(tenant_id, user_id, "active")
    row.extraction_count = 2
    async with storage.session() as session:
        session.add(row)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.parametrize(
    "bad_path", ["C:\\data\\a.jpg", "/app/data/a.jpg", "media\\a.jpg", "../a.jpg"]
)
def test_media_asset_path_must_be_relative_posix(bad_path: str) -> None:
    with pytest.raises(ValueError, match="relative POSIX"):
        MediaAsset(local_path=bad_path)


def test_media_asset_accepts_relative_posix_path() -> None:
    assert MediaAsset(local_path="media/t1/a.jpg").local_path == "media/t1/a.jpg"
