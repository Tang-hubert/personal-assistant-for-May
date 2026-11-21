from datetime import UTC, date, datetime

import pytest

from src.domain.timeutil import taipei_date, taipei_day_utc_range
from tests.fakes import FrozenClock


def test_last_second_before_taipei_midnight() -> None:
    clock = FrozenClock(datetime(2026, 11, 21, 15, 59, 59, tzinfo=UTC))
    assert taipei_date(clock.now()) == date(2026, 11, 21)
    assert taipei_day_utc_range(clock.now()) == (
        datetime(2026, 11, 20, 16, 0, tzinfo=UTC),
        datetime(2026, 11, 21, 16, 0, tzinfo=UTC),
    )


def test_taipei_midnight_starts_next_day() -> None:
    clock = FrozenClock(datetime(2026, 11, 21, 16, 0, 0, tzinfo=UTC))
    assert taipei_date(clock.now()) == date(2026, 11, 22)
    start, end = taipei_day_utc_range(clock.now())
    assert start == datetime(2026, 11, 21, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 22, 16, 0, tzinfo=UTC)
    assert start.tzinfo is UTC and end.tzinfo is UTC


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="aware"):
        taipei_day_utc_range(datetime(2026, 11, 21, 12, 0))
