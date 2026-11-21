import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from src.adapters.storage.repository import SqlStorage
from src.adapters.storage.seed import DEFAULT_TENANT_ID, seed_allowed_users
from tests.fakes import FrozenClock

BUDGET = Decimal("0.35")


def _rows(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(sql).fetchall()


async def test_each_allowed_id_gets_its_own_user_in_the_default_tenant(
    storage: SqlStorage, clock: FrozenClock, db_path: Path
) -> None:
    report = await seed_allowed_users(storage, clock, (111, 222), BUDGET)

    a = await storage.resolve_identity("telegram", "111")
    b = await storage.resolve_identity("telegram", "222")
    assert a is not None and b is not None
    assert a.tenant_id == b.tenant_id == DEFAULT_TENANT_ID
    assert a.user_id != b.user_id
    assert report.tenant_created and report.created == (111, 222)
    assert _rows(db_path, "SELECT id, daily_budget_usd FROM tenants") == [(DEFAULT_TENANT_ID, 0.35)]


async def test_a_second_run_changes_nothing(
    storage: SqlStorage, clock: FrozenClock, db_path: Path
) -> None:
    await seed_allowed_users(storage, clock, (111, 222), BUDGET)
    snapshot = [
        _rows(db_path, f"SELECT * FROM {table} ORDER BY id")
        for table in ("tenants", "users", "channel_identities")
    ]

    report = await seed_allowed_users(storage, clock, (111, 222), Decimal("9"))

    assert not report.tenant_created and report.created == ()
    assert report.existing == (111, 222)
    assert snapshot == [
        _rows(db_path, f"SELECT * FROM {table} ORDER BY id")
        for table in ("tenants", "users", "channel_identities")
    ]


async def test_an_existing_identity_is_left_where_it_is(
    storage: SqlStorage, clock: FrozenClock
) -> None:
    tenant_id = await storage.create_tenant(name="Family")
    user_id = await storage.create_user(tenant_id=tenant_id, display_name="May")
    await storage.link_channel_identity(
        tenant_id=tenant_id, user_id=user_id, channel="telegram", external_user_id="111"
    )

    report = await seed_allowed_users(storage, clock, (111, 333), BUDGET)

    kept = await storage.resolve_identity("telegram", "111")
    new = await storage.resolve_identity("telegram", "333")
    assert kept is not None and (kept.tenant_id, kept.user_id) == (tenant_id, user_id)
    assert new is not None and new.tenant_id == DEFAULT_TENANT_ID
    assert report.created == (333,) and report.existing == (111,)
