"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-21 20:44:52.933752
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import src.adapters.storage.types

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("plan_tier", sa.String(length=32), nullable=False),
        sa.Column("daily_budget_usd", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("budget_alert_80_date", sa.Date(), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("created_at", src.adapters.storage.types.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("preferences_json", sa.JSON(), nullable=False),
        sa.Column("created_at", src.adapters.storage.types.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_users_tenant_id_id"),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_users_tenant_id"), ["tenant_id"], unique=False)

    op.create_table(
        "channel_identities",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("external_user_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", src.adapters.storage.types.UTCDateTime(), nullable=False),
        sa.CheckConstraint("channel IN ('telegram', 'line')", name="ck_channel_identity_channel"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel", "external_user_id", name="uq_channel_identity"),
    )
    op.create_table(
        "knowledge_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("structured_json", sa.JSON(), nullable=False),
        sa.Column("primary_asset_id", sa.String(length=32), nullable=True),
        sa.Column("search_tokens", sa.Text(), nullable=False),
        sa.Column("created_at", src.adapters.storage.types.UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "category IN ('jewelry', 'product', 'lecture', 'item_location', 'crm')",
            name="ck_knowledge_items_category",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("knowledge_items", schema=None) as batch_op:
        batch_op.create_index("ix_knowledge_items_owner", ["tenant_id", "user_id"], unique=False)

    op.create_table(
        "llm_usage_ledger",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("workflow_name", sa.String(length=32), nullable=False),
        sa.Column("created_at", src.adapters.storage.types.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("llm_usage_ledger", schema=None) as batch_op:
        batch_op.create_index(
            "ix_llm_usage_ledger_tenant_day", ["tenant_id", "created_at"], unique=False
        )

    op.create_table(
        "media_batches",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("media_group_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("raw_count", sa.Integer(), nullable=False),
        sa.Column("kept_count", sa.Integer(), nullable=False),
        sa.Column("intent_expires_at", src.adapters.storage.types.UTCDateTime(), nullable=True),
        sa.Column("created_at", src.adapters.storage.types.UTCDateTime(), nullable=False),
        sa.CheckConstraint("channel IN ('telegram', 'line')", name="ck_media_batches_channel"),
        sa.CheckConstraint(
            "status IN ('collecting', 'filtered', 'awaiting_intent', 'done')",
            name="ck_media_batches_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_media_batches_tenant_id_id"),
    )
    op.create_table(
        "topic_sessions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("topic_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("extraction_count", sa.Integer(), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("recent_turns_json", sa.JSON(), nullable=False),
        sa.Column("last_active_at", src.adapters.storage.types.UTCDateTime(), nullable=False),
        sa.Column("created_at", src.adapters.storage.types.UTCDateTime(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'closed')", name="ck_topic_sessions_status"),
        sa.CheckConstraint(
            "topic_type IN ('lecture', 'sales', 'memory', 'ios_help', 'general')",
            name="ck_topic_sessions_topic_type",
        ),
        sa.CheckConstraint("extraction_count BETWEEN 0 AND 1", name="ck_topic_sessions_extraction"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("topic_sessions", schema=None) as batch_op:
        batch_op.create_index(
            "uq_topic_sessions_one_active",
            ["tenant_id", "user_id"],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )

    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("batch_id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("external_file_id", sa.Text(), nullable=True),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("local_path", sa.Text(), nullable=False),
        sa.Column("phash", sa.String(length=32), nullable=True),
        sa.Column("blur_score", sa.Double(), nullable=True),
        sa.Column("is_kept", sa.Boolean(), nullable=False),
        sa.Column("is_blurry", sa.Boolean(), nullable=False),
        sa.Column("created_at", src.adapters.storage.types.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            ["media_batches.tenant_id", "media_batches.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("media_assets", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_media_assets_batch_id"), ["batch_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("media_assets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_media_assets_batch_id"))

    op.drop_table("media_assets")
    with op.batch_alter_table("topic_sessions", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_topic_sessions_one_active",
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )

    op.drop_table("topic_sessions")
    op.drop_table("media_batches")
    with op.batch_alter_table("llm_usage_ledger", schema=None) as batch_op:
        batch_op.drop_index("ix_llm_usage_ledger_tenant_day")

    op.drop_table("llm_usage_ledger")
    with op.batch_alter_table("knowledge_items", schema=None) as batch_op:
        batch_op.drop_index("ix_knowledge_items_owner")

    op.drop_table("knowledge_items")
    op.drop_table("channel_identities")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_tenant_id"))

    op.drop_table("users")
    op.drop_table("tenants")
