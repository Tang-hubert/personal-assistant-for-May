"""knowledge_items FTS5 index over search_tokens (hand-written, SQLite only; ADR-0004)

The index stores its own copy of search_tokens keyed by item_id instead of using
external content: knowledge_items has a TEXT primary key, so its implicit rowid
may be renumbered by VACUUM and would silently desync an external-content index.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = [
    """
    CREATE VIRTUAL TABLE knowledge_items_fts USING fts5(
        search_tokens, item_id UNINDEXED, tokenize = 'unicode61'
    )
    """,
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
    """
    INSERT INTO knowledge_items_fts (search_tokens, item_id)
    SELECT search_tokens, id FROM knowledge_items
    """,
]

DOWNGRADE = [
    "DROP TRIGGER IF EXISTS knowledge_items_fts_au",
    "DROP TRIGGER IF EXISTS knowledge_items_fts_ad",
    "DROP TRIGGER IF EXISTS knowledge_items_fts_ai",
    "DROP TABLE IF EXISTS knowledge_items_fts",
]


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    for statement in DOWNGRADE:
        op.execute(statement)
