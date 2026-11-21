"""The production Clock (ADR-0005): UTC-aware, never process local time."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
