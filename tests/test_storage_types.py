from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import StatementError

from src.adapters.storage.orm import Tenant
from src.adapters.storage.repository import SqlStorage

TAIPEI_OFFSET = timezone(timedelta(hours=8))


async def test_utc_datetime_round_trip_returns_utc_aware(storage: SqlStorage) -> None:
    written = datetime(2026, 11, 21, 9, 30, tzinfo=TAIPEI_OFFSET)
    async with storage.session() as session:
        session.add(Tenant(id="t-rt", name="May", created_at=written))
        await session.commit()

    async with storage.session() as session:
        read = (
            await session.execute(select(Tenant.created_at).where(Tenant.id == "t-rt"))
        ).scalar_one()

    assert read.tzinfo is UTC
    assert read == written
    assert read == datetime(2026, 11, 21, 1, 30, tzinfo=UTC)


async def test_naive_datetime_is_rejected_on_write(storage: SqlStorage) -> None:
    async with storage.session() as session:
        session.add(Tenant(id="t-naive", name="May", created_at=datetime(2026, 11, 21, 9, 30)))
        with pytest.raises(StatementError, match="aware"):
            await session.commit()
