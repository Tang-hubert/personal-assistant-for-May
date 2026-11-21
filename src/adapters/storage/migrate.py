from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
FTS_TABLE_PREFIX = "knowledge_items_fts"


def sync_url(db_path: Path) -> str:
    return f"sqlite:///{db_path.as_posix()}"


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Hide the hand-written FTS5 table and its shadow tables from autogenerate (ADR-0004)."""
    return not (type_ == "table" and name is not None and name.startswith(FTS_TABLE_PREFIX))


def alembic_config(db_path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", MIGRATIONS_DIR.as_posix())
    config.set_main_option("sqlalchemy.url", sync_url(db_path))
    return config


def upgrade_to_head(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(db_path), "head")
