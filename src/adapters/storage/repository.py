"""SQLite implementation of StoragePort. Every query is scoped by tenant_id and user_id,
except identity resolution, which is how those IDs are obtained (ADR-0001)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.adapters.storage.orm import ChannelIdentity, KnowledgeItemRow, Tenant, User
from src.domain.models import Channel, KnowledgeItem, NewKnowledgeItem, ResolvedIdentity
from src.domain.ports import Clock
from src.domain.search_tokens import build_match_query, build_search_tokens

_SEARCH_SQL = text(
    """
    SELECT knowledge_items_fts.item_id
    FROM knowledge_items_fts
    JOIN knowledge_items ON knowledge_items.id = knowledge_items_fts.item_id
    WHERE knowledge_items_fts MATCH :match
      AND knowledge_items.tenant_id = :tenant_id
      AND knowledge_items.user_id = :user_id
    ORDER BY bm25(knowledge_items_fts)
    LIMIT :limit
    """
)


def _to_domain(row: KnowledgeItemRow) -> KnowledgeItem:
    return KnowledgeItem(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        category=row.category,
        title=row.title,
        content_text=row.content_text,
        structured_json=row.structured_json,
        primary_asset_id=row.primary_asset_id,
        created_at=row.created_at,
    )


class SqlStorage:
    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._clock = clock

    def session(self) -> AsyncSession:
        return self._sessions()

    async def create_tenant(self, *, name: str) -> str:
        async with self._sessions() as session:
            tenant = Tenant(name=name, created_at=self._clock.now())
            session.add(tenant)
            await session.commit()
            return tenant.id

    async def create_user(self, *, tenant_id: str, display_name: str) -> str:
        async with self._sessions() as session:
            user = User(
                tenant_id=tenant_id, display_name=display_name, created_at=self._clock.now()
            )
            session.add(user)
            await session.commit()
            return user.id

    async def link_channel_identity(
        self, *, tenant_id: str, user_id: str, channel: Channel, external_user_id: str
    ) -> str:
        async with self._sessions() as session:
            identity = ChannelIdentity(
                tenant_id=tenant_id,
                user_id=user_id,
                channel=channel,
                external_user_id=external_user_id,
                created_at=self._clock.now(),
            )
            session.add(identity)
            await session.commit()
            return identity.id

    async def resolve_identity(
        self, channel: Channel, external_user_id: str
    ) -> ResolvedIdentity | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(ChannelIdentity.tenant_id, ChannelIdentity.user_id).where(
                        ChannelIdentity.channel == channel,
                        ChannelIdentity.external_user_id == external_user_id,
                    )
                )
            ).one_or_none()
        return None if row is None else ResolvedIdentity(tenant_id=row[0], user_id=row[1])

    async def save_knowledge(
        self,
        *,
        tenant_id: str,
        user_id: str,
        item: NewKnowledgeItem,
        keywords: Sequence[str] = (),
    ) -> str:
        async with self._sessions() as session:
            row = KnowledgeItemRow(
                tenant_id=tenant_id,
                user_id=user_id,
                category=item.category,
                title=item.title,
                content_text=item.content_text,
                structured_json=item.structured_json,
                primary_asset_id=item.primary_asset_id,
                search_tokens=build_search_tokens(f"{item.title} {item.content_text}", keywords),
                created_at=self._clock.now(),
            )
            session.add(row)
            await session.commit()
            return row.id

    async def get_knowledge(
        self, *, tenant_id: str, user_id: str, item_id: str
    ) -> KnowledgeItem | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(KnowledgeItemRow).where(
                        KnowledgeItemRow.id == item_id,
                        KnowledgeItemRow.tenant_id == tenant_id,
                        KnowledgeItemRow.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
        return None if row is None else _to_domain(row)

    async def search_knowledge(
        self, *, tenant_id: str, user_id: str, query: str, limit: int = 3
    ) -> list[KnowledgeItem]:
        match = build_match_query(query)
        if match is None:
            return []
        async with self._sessions() as session:
            ids = (
                (
                    await session.execute(
                        _SEARCH_SQL,
                        {
                            "match": match,
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "limit": limit,
                        },
                    )
                )
                .scalars()
                .all()
            )
            rows = (
                (
                    await session.execute(
                        select(KnowledgeItemRow).where(KnowledgeItemRow.id.in_(ids))
                    )
                )
                .scalars()
                .all()
            )
        by_id = {row.id: row for row in rows}
        return [_to_domain(by_id[item_id]) for item_id in ids]
