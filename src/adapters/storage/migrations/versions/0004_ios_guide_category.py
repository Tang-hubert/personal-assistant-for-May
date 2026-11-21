"""knowledge_items.category accepts ios_guide

SQLite cannot alter a CHECK constraint, so the table is rebuilt. The rebuild drops the table the
FTS triggers (0002) hang on, so they are dropped first and recreated after. The FTS rows are keyed
by item_id, not rowid, so they stay valid across the rebuild.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_knowledge_items_category"
OLD_CATEGORIES = "category IN ('jewelry', 'product', 'lecture', 'item_location', 'crm')"
NEW_CATEGORIES = (
    "category IN ('jewelry', 'product', 'lecture', 'item_location', 'crm', 'ios_guide')"
)

TRIGGER_NAMES = ("knowledge_items_fts_ai", "knowledge_items_fts_ad", "knowledge_items_fts_au")
CREATE_TRIGGERS = [
    """
    CREATE TRIGGER knowledge_items_fts_ai AFTER INSERT ON knowledge_items BEGIN
        INSERT INTO knowledge_items_fts (search_tokens, item_id)
        VALUES (new.search_tokens, new.id);
    END
    """,
    """
    CREATE TRIGGER knowledge_items_fts_ad AFTER DELETE ON knowledge_items BEGIN
        DELETE FROM knowledge_items_fts WHERE item_id = old.id;
    END
    """,
    """
    CREATE TRIGGER knowledge_items_fts_au AFTER UPDATE OF search_tokens ON knowledge_items BEGIN
        DELETE FROM knowledge_items_fts WHERE item_id = old.id;
        INSERT INTO knowledge_items_fts (search_tokens, item_id)
        VALUES (new.search_tokens, new.id);
    END
    """,
]


def _replace_check(condition: str) -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    if sqlite:
        for name in TRIGGER_NAMES:
            op.execute(f"DROP TRIGGER IF EXISTS {name}")
    with op.batch_alter_table("knowledge_items", recreate="always") as batch_op:
        batch_op.drop_constraint(CONSTRAINT, type_="check")
        batch_op.create_check_constraint(CONSTRAINT, condition)
    if sqlite:
        for statement in CREATE_TRIGGERS:
            op.execute(statement)


def upgrade() -> None:
    _replace_check(NEW_CATEGORIES)


def downgrade() -> None:
    # Deleted while the delete trigger still exists, so their FTS rows go with them.
    op.execute("DELETE FROM knowledge_items WHERE category = 'ios_guide'")
    _replace_check(OLD_CATEGORIES)
