# ADR-0005: Time and Path Normalization Across Windows Dev and Linux Prod

- **Status**: Accepted (Architect ruling v1.3, 2026-11-21)
- **Source findings**: Review 1.4, 1.5, 1.7 (and 1.10 for the Python pin)

## Context
- Windows does not honor IANA `TZ` values the way Linux does, has no `time.tzset()`, and ships no IANA database for `zoneinfo`. Slim Docker images may also lack system tzdata. So "Taipei today" for FinOps could differ by platform.
- SQLAlchemy `DateTime(timezone=True)` on SQLite stores naive strings. Values read back are naive, and comparing them with aware datetimes raises.
- `media_assets.local_path` and `IncomingEvent.media_bytes_ref: Path` would persist Windows absolute paths (`C:\...`) during development.

## Decision
1. Add the `tzdata` dependency. Pin `.python-version` to `3.12`.
2. Every timestamp column uses a custom `UTCDateTime` TypeDecorator. It converts to UTC on write and attaches `tzinfo=UTC` on read, identically on SQLite and Postgres.
3. Inject a `Clock` port (`now() -> datetime`, UTC-aware). No code calls `datetime.now()` / `time.time()` for business logic directly. Tests freeze the clock.
4. "Taipei today" is computed in Python with `ZoneInfo("Asia/Taipei")` and converted to a `[start_utc, end_utc)` range for queries. Never rely on process local time or `TZ`.
5. `media_assets.local_path` stores a POSIX string relative to `DATA_DIR`. Only the storage adapter joins it to an absolute `Path`. The domain passes `asset_id`, never `Path`.

## Invariants
1. No naive `datetime` crosses a repository boundary.
2. Business logic obtains time only from `Clock`.
3. No absolute or backslash path is ever stored in the DB.
4. `IncomingEvent` / `BotResponse` contain no `Path` fields.

## Consequences
- `TZ=Asia/Taipei` in `docker-compose.yml` and `scripts/test.py` only affects logs. Correctness does not depend on it.
- A dev DB copied to prod stays valid (relative paths, UTC times).
- Small amount of boilerplate (TypeDecorator, Clock), written once in T1.

## Verification
- T1: `UTCDateTime` round-trip returns UTC-aware values on SQLite.
- T1: Taipei-day → UTC-range helper with a frozen `Clock` around the 00:00 Asia/Taipei boundary (= 16:00 UTC the previous day).
- T1/T5: the container test run (`docker compose run --rm app python scripts/test.py`) passes the same suite on Linux.
- T5: stored `local_path` is relative POSIX when the adapter runs on Windows.
