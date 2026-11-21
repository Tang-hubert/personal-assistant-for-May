"""Taipei-day arithmetic (ADR-0005). Never reads process local time or TZ."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")


def _require_aware(instant: datetime) -> None:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("expected a timezone-aware datetime")


def taipei_date(instant: datetime) -> date:
    _require_aware(instant)
    return instant.astimezone(TAIPEI).date()


def taipei_day_utc_range(instant: datetime) -> tuple[datetime, datetime]:
    """Return the half-open UTC range [start, end) of the Taipei day containing `instant`."""
    day = taipei_date(instant)
    start = datetime.combine(day, time.min, tzinfo=TAIPEI)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=TAIPEI)
    return start.astimezone(UTC), end.astimezone(UTC)
