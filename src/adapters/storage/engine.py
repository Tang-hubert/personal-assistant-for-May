from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DB_FILENAME = "app.db"


def db_path_for(data_dir: Path) -> Path:
    return data_dir / DB_FILENAME


def async_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_engine(db_path: Path) -> AsyncEngine:
    engine = create_async_engine(async_url(db_path))
    event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
    return engine
