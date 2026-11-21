"""media assets exist before their batch: nullable batch_id, owner FK, batch reply address

media_assets had no writer before T2, so adding the NOT NULL user_id needs no backfill.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_batches", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reply_address", sa.Text(), nullable=True))
    with op.batch_alter_table("media_assets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(length=32), nullable=False))
        batch_op.alter_column("batch_id", existing_type=sa.String(length=32), nullable=True)
        batch_op.create_foreign_key(
            "fk_media_assets_owner", "users", ["tenant_id", "user_id"], ["tenant_id", "id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("media_assets", schema=None) as batch_op:
        batch_op.drop_constraint("fk_media_assets_owner", type_="foreignkey")
        batch_op.alter_column("batch_id", existing_type=sa.String(length=32), nullable=False)
        batch_op.drop_column("user_id")
    with op.batch_alter_table("media_batches", schema=None) as batch_op:
        batch_op.drop_column("reply_address")
