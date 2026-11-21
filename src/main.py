"""Entry point. T1 only validates settings and migrates the DB; T5 wires the Telegram adapter."""

from __future__ import annotations

import logging

from src.adapters.storage.engine import db_path_for
from src.adapters.storage.migrate import upgrade_to_head
from src.config import Settings

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    db_path = db_path_for(settings.DATA_DIR)
    upgrade_to_head(db_path)
    log.info("database migrated at %s (APP_ENV=%s)", db_path.as_posix(), settings.APP_ENV)


if __name__ == "__main__":
    main()
