"""SQLite implementation of SessionStorePort. Every query is scoped by tenant_id and user_id.

At most one active session per user is guaranteed by the partial unique index
uq_topic_sessions_one_active, not by this code (ARCHITECTURE §4.5); the router's per-user lock
only keeps one process from tripping over it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from src.adapters.storage.orm import TopicSession, new_id
from src.domain.budget import Turn
from src.domain.ports import Clock
from src.domain.session import TopicSessionState, TopicType

ACTIVE = "active"
CLOSED = "closed"


def _state(row: TopicSession) -> TopicSessionState:
    return TopicSessionState.model_validate(
        {
            "id": row.id,
            "topic_type": row.topic_type,
            "turn_count": row.turn_count,
            "extraction_count": row.extraction_count,
            "state_json": row.state_json,
            "recent_turns": tuple(Turn.model_validate(turn) for turn in row.recent_turns_json),
            "last_active_at": row.last_active_at,
        }
    )


class SqlSessionStore:
    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._clock = clock

    async def get_active(self, *, tenant_id: str, user_id: str) -> TopicSessionState | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(TopicSession).where(
                        TopicSession.tenant_id == tenant_id,
                        TopicSession.user_id == user_id,
                        TopicSession.status == ACTIVE,
                    )
                )
            ).scalar_one_or_none()
        return None if row is None else _state(row)

    async def open_fresh(
        self,
        *,
        tenant_id: str,
        user_id: str,
        topic_type: TopicType,
        state_json: dict[str, Any] | None = None,
    ) -> TopicSessionState:
        """Close the active session and insert an empty one in one transaction. If a concurrent
        writer's insert wins the partial unique index, return that session instead."""
        now = self._clock.now()
        fresh = TopicSessionState(
            id=new_id(),
            topic_type=topic_type,
            turn_count=0,
            extraction_count=0,
            state_json=state_json or {},
            recent_turns=(),
            last_active_at=now,
        )
        async with self._sessions() as session:
            await session.execute(
                update(TopicSession)
                .where(
                    TopicSession.tenant_id == tenant_id,
                    TopicSession.user_id == user_id,
                    TopicSession.status == ACTIVE,
                )
                .values(status=CLOSED)
            )
            session.add(
                TopicSession(
                    id=fresh.id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    topic_type=topic_type,
                    status=ACTIVE,
                    state_json=fresh.state_json,
                    recent_turns_json=[],
                    last_active_at=now,
                    created_at=now,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await self.get_active(tenant_id=tenant_id, user_id=user_id)
                if existing is None:
                    raise
                return existing
        return fresh

    async def save(self, *, tenant_id: str, user_id: str, state: TopicSessionState) -> bool:
        # Re-validate: model_construct can build a state that skipped the invariant check.
        checked = TopicSessionState.model_validate(dict(state))
        async with self._sessions() as session:
            result = await session.execute(
                update(TopicSession)
                .where(
                    TopicSession.id == checked.id,
                    TopicSession.tenant_id == tenant_id,
                    TopicSession.user_id == user_id,
                    TopicSession.status == ACTIVE,
                )
                .values(
                    topic_type=checked.topic_type,
                    turn_count=checked.turn_count,
                    extraction_count=checked.extraction_count,
                    state_json=checked.state_json,
                    recent_turns_json=[turn.model_dump() for turn in checked.recent_turns],
                    last_active_at=checked.last_active_at,
                )
            )
            await session.commit()
            return result.rowcount == 1  # type: ignore[attr-defined,no-any-return]

    async def close(self, *, tenant_id: str, user_id: str, session_id: str) -> None:
        async with self._sessions() as session:
            await session.execute(
                update(TopicSession)
                .where(
                    TopicSession.id == session_id,
                    TopicSession.tenant_id == tenant_id,
                    TopicSession.user_id == user_id,
                )
                .values(status=CLOSED)
            )
            await session.commit()
