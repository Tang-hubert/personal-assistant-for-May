"""Startup seeding: one person per allowlisted Telegram account, all in the default tenant.
Converges on every run, so a restart after editing the allowlist only adds the new accounts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from src.adapters.storage.orm import ChannelIdentity, Tenant, User
from src.adapters.storage.repository import SqlStorage
from src.domain.ports import Clock

DEFAULT_TENANT_ID = "default"


@dataclass(frozen=True)
class SeedReport:
    tenant_created: bool
    created: tuple[int, ...]
    existing: tuple[int, ...]


async def seed_allowed_users(
    storage: SqlStorage, clock: Clock, allowed_ids: Sequence[int], default_budget: Decimal
) -> SeedReport:
    """The budget is written only when the tenant is created; later edits stay in the DB."""
    async with storage.session() as session:
        tenant_created = await session.get(Tenant, DEFAULT_TENANT_ID) is None
        if tenant_created:
            session.add(
                Tenant(
                    id=DEFAULT_TENANT_ID,
                    name="default",
                    daily_budget_usd=default_budget,
                    created_at=clock.now(),
                )
            )
            await session.commit()

    created: list[int] = []
    existing: list[int] = []
    for telegram_id in allowed_ids:
        if await storage.resolve_identity("telegram", str(telegram_id)) is not None:
            existing.append(telegram_id)
            continue
        async with storage.session() as session:
            user = User(
                tenant_id=DEFAULT_TENANT_ID,
                display_name=f"telegram {telegram_id}",
                created_at=clock.now(),
            )
            session.add(user)
            await session.flush()
            session.add(
                ChannelIdentity(
                    tenant_id=DEFAULT_TENANT_ID,
                    user_id=user.id,
                    channel="telegram",
                    external_user_id=str(telegram_id),
                    created_at=clock.now(),
                )
            )
            await session.commit()
        created.append(telegram_id)
    return SeedReport(tenant_created, tuple(created), tuple(existing))
