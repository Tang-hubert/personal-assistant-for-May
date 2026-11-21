"""Entry point: settings -> migrations -> PTB long polling. PTB owns the event loop and the stop
signals; the runtime starts in post_init and drains in post_stop, while the bot can still send."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.error import InvalidToken
from telegram.ext import Application, ApplicationBuilder, ContextTypes, TypeHandler

from src.adapters.clock import SystemClock
from src.adapters.llm.deepseek_adapter import DeepSeekDirectAdapter, create_deepseek_client
from src.adapters.llm.gemini_adapter import GeminiDirectAdapter, create_gemini_client
from src.adapters.storage.engine import db_path_for
from src.adapters.storage.migrate import upgrade_to_head
from src.adapters.telegram.bot_api import PtbBotApi
from src.bootstrap import build_runtime, drain, start, stop
from src.config import Settings
from src.domain.ports import LLMPort, MediaStorePort, Provider

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query"]


class RedactSecrets(logging.Filter):
    """PTB logs InvalidToken with its traceback, and that message holds the whole bot token."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = self._redact(record.getMessage()), None
        if record.exc_info:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redact(record.stack_info)
        return True


def configure_logging(*secrets: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO, and a Bot API URL contains the bot token.
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    redact = RedactSecrets(secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)


def load_settings() -> Settings:
    settings = Settings()
    if not settings.TELEGRAM_ALLOWED_USER_IDS:
        raise SystemExit("TELEGRAM_ALLOWED_USER_IDS is empty: nobody could use the bot")
    return settings


def build_application(settings: Settings) -> Application:  # type: ignore[type-arg]
    gemini_client = create_gemini_client(settings.GEMINI_API_KEY)
    deepseek_client = create_deepseek_client(settings.DEEPSEEK_API_KEY)

    def providers(media: MediaStorePort) -> dict[Provider, LLMPort]:
        return {
            "gemini": GeminiDirectAdapter(gemini_client, settings.GEMINI_MODEL, media),
            "deepseek": DeepSeekDirectAdapter(deepseek_client, settings.DEEPSEEK_MODEL),
        }

    async def on_start(_app: Application) -> None:  # type: ignore[type-arg]
        await start(runtime)

    async def on_stop(_app: Application) -> None:  # type: ignore[type-arg]
        await drain(runtime)

    async def on_shutdown(_app: Application) -> None:  # type: ignore[type-arg]
        await stop(runtime)
        await gemini_client.aio.aclose()
        await deepseek_client.close()

    async def on_update(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await runtime.adapter.handle_update(update)

    application = (
        ApplicationBuilder()
        .token(settings.TELEGRAM_BOT_TOKEN.get_secret_value())
        .post_init(on_start)
        .post_stop(on_stop)
        .post_shutdown(on_shutdown)
        .build()
    )
    runtime = build_runtime(settings, PtbBotApi(application.bot), SystemClock(), providers)
    application.add_handler(TypeHandler(Update, on_update))
    return application


def main() -> None:
    settings = load_settings()
    configure_logging(settings.TELEGRAM_BOT_TOKEN.get_secret_value())
    db_path = db_path_for(settings.DATA_DIR)
    upgrade_to_head(db_path)
    log.info("database migrated at %s (APP_ENV=%s)", db_path.as_posix(), settings.APP_ENV)
    try:
        build_application(settings).run_polling(allowed_updates=ALLOWED_UPDATES)
    except InvalidToken:
        raise SystemExit("TELEGRAM_BOT_TOKEN was rejected by Telegram; check .env") from None


if __name__ == "__main__":
    main()
