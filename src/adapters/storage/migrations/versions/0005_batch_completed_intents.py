"""media_batches.completed_intents: the intents already finished for a batch

A batch stays awaiting_intent after a workflow finishes, so May can tap a different intent on
the same photos within the TTL; the finished intents are what make a repeated tap a no-op.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_batches") as batch_op:
        batch_op.add_column(
            sa.Column("completed_intents", sa.JSON(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    with op.batch_alter_table("media_batches") as batch_op:
        batch_op.drop_column("completed_intents")
