"""SQLAlchemy schema (ARCHITECTURE §5). Every user-owned row carries (tenant_id, user_id),
and a composite foreign key to users(tenant_id, id) makes a cross-tenant row unrepresentable."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

from src.adapters.storage.types import UTCDateTime


def new_id() -> str:
    return uuid.uuid4().hex


def _owner_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"])


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: UTCDateTime(),
        dict[str, Any]: JSON,
        list[Any]: JSON,
    }


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(Text)
    plan_tier: Mapped[str] = mapped_column(String(32), default="family")
    daily_budget_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("0.20"))
    budget_alert_80_date: Mapped[date | None] = mapped_column(Date)
    config_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime]


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_users_tenant_id_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    display_name: Mapped[str] = mapped_column(Text)
    preferences_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime]


CHANNELS = ("telegram", "line")


class ChannelIdentity(Base):
    __tablename__ = "channel_identities"
    __table_args__ = (
        UniqueConstraint("channel", "external_user_id", name="uq_channel_identity"),
        CheckConstraint(_in("channel", CHANNELS), name="ck_channel_identity_channel"),
        _owner_fk(),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(16))
    external_user_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime]


TOPIC_TYPES = ("lecture", "sales", "memory", "ios_help", "general")
SESSION_STATUSES = ("active", "closed")


class TopicSession(Base):
    __tablename__ = "topic_sessions"
    __table_args__ = (
        Index(
            "uq_topic_sessions_one_active",
            "tenant_id",
            "user_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        CheckConstraint(_in("topic_type", TOPIC_TYPES), name="ck_topic_sessions_topic_type"),
        CheckConstraint(_in("status", SESSION_STATUSES), name="ck_topic_sessions_status"),
        CheckConstraint("extraction_count BETWEEN 0 AND 1", name="ck_topic_sessions_extraction"),
        _owner_fk(),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(String(32))
    topic_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    turn_count: Mapped[int] = mapped_column(default=0)
    extraction_count: Mapped[int] = mapped_column(default=0)
    state_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    recent_turns_json: Mapped[list[Any]] = mapped_column(default=list)
    last_active_at: Mapped[datetime]
    created_at: Mapped[datetime]


BATCH_STATUSES = ("collecting", "filtered", "awaiting_intent", "done")


class MediaBatch(Base):
    __tablename__ = "media_batches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_media_batches_tenant_id_id"),
        CheckConstraint(_in("status", BATCH_STATUSES), name="ck_media_batches_status"),
        CheckConstraint(_in("channel", CHANNELS), name="ck_media_batches_channel"),
        _owner_fk(),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(16))
    media_group_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    raw_count: Mapped[int] = mapped_column(default=0)
    kept_count: Mapped[int] = mapped_column(default=0)
    intent_expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime]


class MediaAsset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "batch_id"], ["media_batches.tenant_id", "media_batches.id"]
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(String(32), index=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    external_file_id: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(16))
    local_path: Mapped[str] = mapped_column(Text)
    phash: Mapped[str | None] = mapped_column(String(32))
    blur_score: Mapped[float | None]
    is_kept: Mapped[bool] = mapped_column(default=False)
    is_blurry: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime]

    @validates("local_path")
    def _relative_posix(self, _key: str, value: str) -> str:
        path = PurePosixPath(value)
        if "\\" in value or ":" in value or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"local_path must be a relative POSIX path under DATA_DIR: {value!r}")
        return value


KNOWLEDGE_CATEGORIES = ("jewelry", "product", "lecture", "item_location", "crm")


class KnowledgeItemRow(Base):
    __tablename__ = "knowledge_items"
    __table_args__ = (
        Index("ix_knowledge_items_owner", "tenant_id", "user_id"),
        CheckConstraint(_in("category", KNOWLEDGE_CATEGORIES), name="ck_knowledge_items_category"),
        _owner_fk(),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text)
    content_text: Mapped[str] = mapped_column(Text)
    structured_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    primary_asset_id: Mapped[str | None] = mapped_column(String(32))
    search_tokens: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]


class LlmUsage(Base):
    __tablename__ = "llm_usage_ledger"
    __table_args__ = (
        Index("ix_llm_usage_ledger_tenant_day", "tenant_id", "created_at"),
        _owner_fk(),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    prompt_tokens: Mapped[int]
    completion_tokens: Mapped[int]
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    workflow_name: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime]
