import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.adapters.storage.engine import create_engine
from src.adapters.storage.session_repository import SqlSessionStore
from src.domain.budget import Turn
from src.domain.session import SessionSlots, TopicSessionState, apply_extraction, commit_turn
from tests.fakes import FrozenClock, Owner


@pytest.fixture
async def store(db_path: Path, clock: FrozenClock) -> AsyncIterator[SqlSessionStore]:
    engine = create_engine(db_path)
    yield SqlSessionStore(engine, clock)
    await engine.dispose()


def _statuses(db_path: Path, owner: Owner) -> list[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT status FROM topic_sessions WHERE tenant_id = ? AND user_id = ? ORDER BY rowid",
            (owner.tenant_id, owner.user_id),
        ).fetchall()
    return [row[0] for row in rows]


async def test_open_fresh_starts_empty_and_closes_the_previous_session(
    store: SqlSessionStore, owner: Owner, db_path: Path, clock: FrozenClock
) -> None:
    first = await store.open_fresh(
        tenant_id=owner.tenant_id, user_id=owner.user_id, topic_type="sales"
    )
    assert (first.turn_count, first.extraction_count, first.recent_turns) == (0, 0, ())
    assert first.last_active_at == clock.now()

    second = await store.open_fresh(
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        topic_type="general",
        state_json={"last_artifact_id": "k1", "workflow": "sales_copy"},
    )
    assert second.id != first.id
    assert second.state_json == {"last_artifact_id": "k1", "workflow": "sales_copy"}
    assert _statuses(db_path, owner) == ["closed", "active"]
    assert await store.get_active(tenant_id=owner.tenant_id, user_id=owner.user_id) == second


async def test_save_round_trips_turns_slots_and_activity(
    store: SqlSessionStore, owner: Owner, clock: FrozenClock
) -> None:
    state = await store.open_fresh(
        tenant_id=owner.tenant_id, user_id=owner.user_id, topic_type="general"
    )
    later = clock.now() + timedelta(minutes=5)
    state = commit_turn(state, "翡翠放哪", "在抽屜", later)
    state = apply_extraction(state, SessionSlots(topic="翡翠"))
    state = commit_turn(state, "謝謝", "不客氣", later)

    assert await store.save(tenant_id=owner.tenant_id, user_id=owner.user_id, state=state)
    loaded = await store.get_active(tenant_id=owner.tenant_id, user_id=owner.user_id)
    assert loaded == state
    assert loaded is not None
    assert loaded.recent_turns == (
        Turn(role="user", text="謝謝"),
        Turn(role="assistant", text="不客氣"),
    )


async def test_save_refuses_a_state_that_breaks_the_invariant(
    store: SqlSessionStore, owner: Owner
) -> None:
    state = await store.open_fresh(
        tenant_id=owner.tenant_id, user_id=owner.user_id, topic_type="general"
    )
    seven = tuple(Turn(role="user", text="hi") for _ in range(7))
    smuggled = TopicSessionState.model_construct(**{**dict(state), "recent_turns": seven})
    with pytest.raises(ValidationError):
        await store.save(tenant_id=owner.tenant_id, user_id=owner.user_id, state=smuggled)
    loaded = await store.get_active(tenant_id=owner.tenant_id, user_id=owner.user_id)
    assert loaded is not None
    assert loaded.recent_turns == ()


async def test_save_after_close_reports_false(store: SqlSessionStore, owner: Owner) -> None:
    state = await store.open_fresh(
        tenant_id=owner.tenant_id, user_id=owner.user_id, topic_type="general"
    )
    await store.close(tenant_id=owner.tenant_id, user_id=owner.user_id, session_id=state.id)
    assert not await store.save(tenant_id=owner.tenant_id, user_id=owner.user_id, state=state)
    assert await store.get_active(tenant_id=owner.tenant_id, user_id=owner.user_id) is None


async def test_other_owners_cannot_see_or_touch_a_session(
    store: SqlSessionStore, owner: Owner, other_owner: Owner, db_path: Path
) -> None:
    state = await store.open_fresh(
        tenant_id=owner.tenant_id, user_id=owner.user_id, topic_type="general"
    )
    assert await store.get_active(tenant_id=other_owner.tenant_id, user_id=owner.user_id) is None
    await store.close(tenant_id=other_owner.tenant_id, user_id=owner.user_id, session_id=state.id)
    assert not await store.save(tenant_id=other_owner.tenant_id, user_id=owner.user_id, state=state)
    assert _statuses(db_path, owner) == ["active"]


async def test_concurrent_open_fresh_on_two_connections_leaves_one_active(
    db_path: Path, clock: FrozenClock, owner: Owner
) -> None:
    engines = [create_engine(db_path), create_engine(db_path)]
    stores = [SqlSessionStore(engine, clock) for engine in engines]
    try:
        opened = await asyncio.gather(
            *(
                stores[n % 2].open_fresh(
                    tenant_id=owner.tenant_id, user_id=owner.user_id, topic_type="general"
                )
                for n in range(10)
            )
        )
    finally:
        for engine in engines:
            await engine.dispose()

    statuses = _statuses(db_path, owner)
    assert statuses.count("active") == 1
    assert len(opened) == 10
