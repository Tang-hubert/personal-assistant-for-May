import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from src.adapters.storage.migrate import alembic_config, include_object, sync_url
from src.adapters.storage.orm import Base

EXPECTED_TABLES = {
    "tenants",
    "users",
    "channel_identities",
    "topic_sessions",
    "media_batches",
    "media_assets",
    "knowledge_items",
    "llm_usage_ledger",
}


def test_upgrade_creates_all_tables_and_fts(db_path: Path) -> None:
    engine = create_engine(sync_url(db_path))
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    assert tables >= EXPECTED_TABLES
    assert "knowledge_items_fts" in tables


def test_autogenerate_sees_no_drift_and_no_fts_drops(db_path: Path) -> None:
    engine = create_engine(sync_url(db_path))
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"include_object": include_object, "compare_type": True}
        )
        diff = compare_metadata(context, Base.metadata)
    engine.dispose()
    assert diff == []


def test_downgrade_to_base_removes_everything(db_path: Path) -> None:
    command.downgrade(alembic_config(db_path), "base")
    engine = create_engine(sync_url(db_path))
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    assert tables <= {"alembic_version"}


def _fts_hits(db_path: Path, token: str) -> list[tuple[str]]:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT item_id FROM knowledge_items_fts WHERE knowledge_items_fts MATCH ?",
            (f'"{token}"',),
        ).fetchall()


def _insert_item(db_path: Path, item_id: str, category: str, tokens: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        tenant = conn.execute("SELECT id FROM tenants").fetchone()
        if tenant is None:
            conn.execute(
                "INSERT INTO tenants (id, name, plan_tier, daily_budget_usd, config_json,"
                " created_at) VALUES ('t1', 'May', 'family', 0.2, '{}', '2026-11-21')"
            )
            conn.execute(
                "INSERT INTO users (id, tenant_id, display_name, preferences_json, created_at)"
                " VALUES ('u1', 't1', 'May', '{}', '2026-11-21')"
            )
        conn.execute(
            "INSERT INTO knowledge_items (id, tenant_id, user_id, category, title, content_text,"
            " structured_json, search_tokens, created_at)"
            " VALUES (?, 't1', 'u1', ?, '', '', '{}', ?, '2026-11-21')",
            (item_id, category, tokens),
        )
        conn.commit()


def test_0004_accepts_ios_guide_and_the_rebuilt_table_keeps_its_fts_triggers(
    db_path: Path,
) -> None:
    command.downgrade(alembic_config(db_path), "0003")
    _insert_item(db_path, "old", "item_location", "翡翠")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_item(db_path, "early", "ios_guide", "截圖")

    command.upgrade(alembic_config(db_path), "head")
    _insert_item(db_path, "guide", "ios_guide", "截圖")

    assert _fts_hits(db_path, "截圖") == [("guide",)]
    assert _fts_hits(db_path, "翡翠") == [("old",)]
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("UPDATE knowledge_items SET search_tokens = '珍珠' WHERE id = 'old'")
        conn.execute("DELETE FROM knowledge_items WHERE id = 'guide'")
        conn.commit()
    assert _fts_hits(db_path, "珍珠") == [("old",)]
    assert _fts_hits(db_path, "翡翠") == _fts_hits(db_path, "截圖") == []


def test_0004_downgrade_drops_ios_guide_rows_and_their_index_entries(db_path: Path) -> None:
    _insert_item(db_path, "guide", "ios_guide", "截圖")
    _insert_item(db_path, "place", "item_location", "翡翠")

    command.downgrade(alembic_config(db_path), "0003")

    with closing(sqlite3.connect(db_path)) as conn:
        ids = conn.execute("SELECT id FROM knowledge_items").fetchall()
    assert ids == [("place",)]
    assert _fts_hits(db_path, "截圖") == []
    assert _fts_hits(db_path, "翡翠") == [("place",)]
    with pytest.raises(sqlite3.IntegrityError):
        _insert_item(db_path, "again", "ios_guide", "截圖")
