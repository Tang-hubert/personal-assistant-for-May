from pathlib import Path

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
