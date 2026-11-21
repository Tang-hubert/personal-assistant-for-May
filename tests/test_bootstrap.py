import json
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from telegram.request import HTTPXRequest

from src.adapters.storage.engine import db_path_for
from src.adapters.storage.migrate import upgrade_to_head
from src.bootstrap import Runtime, build_runtime, start, stop
from src.config import Settings
from src.domain import replies
from src.domain.media import Intent
from src.domain.ports import LLMRequest
from src.main import build_application, configure_logging, main
from tests import imagegen
from tests.fakes import FakeLLM, FakeNotifier, FrozenClock
from tests.telegram_helpers import (
    MAY_ID,
    FakeBotApi,
    callback_update,
    photo_update,
    text_update,
)

ENV = {
    "GEMINI_API_KEY": "test-gemini",
    "GEMINI_MODEL": "gemini-3.8-flash",
    "DEEPSEEK_API_KEY": "test-deepseek",
    "DEEPSEEK_MODEL": "deepseek-flash",
    "TELEGRAM_BOT_TOKEN": "123:secret-token",
    "DISCORD_OPS_WEBHOOK_URL": "https://discord.invalid/webhook",
}
REPLIES = {
    "route": json.dumps({"route": "memory", "topic_shift": False}),
    "memory_stash": json.dumps(
        {"item": "翡翠手鍊", "location": "紅櫃子", "keywords": ["翡翠"]}, ensure_ascii=False
    ),
    "memory_parse": json.dumps(
        {"action": "query", "item": "翡翠", "location": "", "keywords": ["翡翠"]},
        ensure_ascii=False,
    ),
    "memory_answer": json.dumps({"bullets": ["翡翠手鍊在紅櫃子"], "pick": 1}, ensure_ascii=False),
}


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        **ENV,
        "TELEGRAM_ALLOWED_USER_IDS": str(MAY_ID),
        "DATA_DIR": tmp_path / "data",
        "BATCH_DEBOUNCE_SECONDS": 0.05,
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)


def _reply(request: LLMRequest) -> str:
    return REPLIES[request.workflow_name]


class Assistant:
    def __init__(self, tmp_path: Path, clock: FrozenClock, **overrides: Any) -> None:
        settings = _settings(tmp_path, **overrides)
        upgrade_to_head(db_path_for(settings.DATA_DIR))
        self.bot = FakeBotApi()
        self.llm = FakeLLM(respond=_reply)
        self.runtime: Runtime = build_runtime(
            settings,
            self.bot,
            clock,
            lambda media: {"gemini": self.llm, "deepseek": self.llm},
            notifier=FakeNotifier(),
        )

    def workflows(self) -> list[str]:
        return [request.workflow_name for request in self.llm.calls]


@pytest.fixture
async def assistant(tmp_path: Path, clock: FrozenClock) -> AsyncIterator[Assistant]:
    app = Assistant(tmp_path, clock)
    await start(app.runtime)
    yield app
    await stop(app.runtime)


async def test_start_recovers_an_interrupted_burst_once_and_reseeding_changes_nothing(
    assistant: Assistant,
) -> None:
    runtime = assistant.runtime
    may = await runtime.storage.resolve_identity("telegram", str(MAY_ID))
    assert may is not None
    asset = await runtime.media.save_asset(
        tenant_id=may.tenant_id,
        user_id=may.user_id,
        data=imagegen.jpeg(imagegen.sharp(5)),
        media_type="image",
        ext="jpg",
    )
    batch_id = await runtime.media.open_batch(
        tenant_id=may.tenant_id,
        user_id=may.user_id,
        channel="telegram",
        media_group_id=None,
        reply_address=str(MAY_ID),
    )
    await runtime.media.attach_assets(
        tenant_id=may.tenant_id, user_id=may.user_id, batch_id=batch_id, asset_ids=[asset]
    )

    await start(runtime)
    await start(runtime)

    (prompt,) = assistant.bot.sent
    assert prompt.chat_id == MAY_ID
    assert prompt.markup is not None
    assert batch_id in str(prompt.markup.inline_keyboard[0][0].callback_data)
    assert await runtime.storage.resolve_identity("telegram", str(MAY_ID)) == may


async def test_a_burst_then_the_box_button_then_a_question_comes_back_with_the_photo(
    assistant: Assistant,
) -> None:
    bot, adapter = assistant.bot, assistant.runtime.adapter
    for n in range(2):
        bot.files[f"p{n}"] = imagegen.jpeg(imagegen.sharp(300 + n))
        await adapter.handle_update(photo_update(file_id=f"p{n}"))
    await assistant.runtime.aggregator.drain()

    (prompt,) = bot.sent
    assert prompt.markup is not None
    buttons = [row[0] for row in prompt.markup.inline_keyboard]
    assert len(buttons) == 4
    box = next(
        str(b.callback_data) for b in buttons if str(b.callback_data).endswith(Intent.MEMORY)
    )

    await adapter.handle_update(callback_update(box))
    await adapter.handle_update(text_update("我的翡翠放哪了"))

    assert assistant.workflows() == ["memory_stash", "route", "memory_parse", "memory_answer"]
    assert len(bot.sent) == 3
    stashed, answer = bot.sent[1], bot.sent[2]
    assert "記好了" in stashed.text
    assert answer.kind == "photo" and answer.chat_id == MAY_ID
    assert answer.photo in bot.files.values()
    assert "翡翠手鍊在紅櫃子" in answer.text


async def test_a_burst_with_failed_downloads_gets_one_notice_and_one_prompt(
    assistant: Assistant,
) -> None:
    bot, adapter = assistant.bot, assistant.runtime.adapter
    bot.files["p0"] = imagegen.jpeg(imagegen.sharp(300))
    await adapter.handle_update(photo_update(file_id="p0", media_group_id="album"))
    bot.fail_download = True
    for n in range(1, 5):
        await adapter.handle_update(photo_update(file_id=f"p{n}", media_group_id="album"))
    await assistant.runtime.aggregator.drain()

    notice, prompt = bot.sent
    assert replies.photo_not_received().title in notice.text
    assert prompt.markup is not None and len(prompt.markup.inline_keyboard) == 4


async def test_stop_drains_a_burst_in_its_debounce_window_before_closing(
    tmp_path: Path, clock: FrozenClock
) -> None:
    app = Assistant(tmp_path, clock, BATCH_DEBOUNCE_SECONDS=0.3)
    await start(app.runtime)
    app.bot.files["p"] = imagegen.jpeg(imagegen.sharp(77))
    await app.runtime.adapter.handle_update(photo_update(file_id="p"))
    assert app.bot.sent == []

    await stop(app.runtime)

    (prompt,) = app.bot.sent
    assert prompt.markup is not None and len(prompt.markup.inline_keyboard) == 4
    assert app.runtime.http_client.is_closed


def _set_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, allowed: str) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", allowed)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)  # so no .env file can be read


def test_an_empty_allowlist_stops_before_migrating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env(monkeypatch, tmp_path, "")

    with pytest.raises(SystemExit, match="TELEGRAM_ALLOWED_USER_IDS"):
        main()

    assert not (tmp_path / "data").exists()


@pytest.fixture
def restore_http_loggers() -> Iterator[None]:
    levels = {name: logging.getLogger(name).level for name in ("httpx", "httpx2")}
    filters = {handler: list(handler.filters) for handler in logging.getLogger().handlers}
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    for handler, kept in filters.items():
        handler.filters = kept


@pytest.mark.usefixtures("restore_http_loggers")
def test_request_urls_with_the_bot_token_never_reach_the_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging()
    caplog.set_level(logging.INFO)

    logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/bot123:secret")

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpx2").getEffectiveLevel() >= logging.WARNING
    assert "secret" not in caplog.text


async def test_the_application_wires_lifecycle_hooks_and_shuts_down_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env(monkeypatch, tmp_path, str(MAY_ID))
    settings = Settings()
    upgrade_to_head(db_path_for(settings.DATA_DIR))

    app = build_application(settings)

    assert app.post_init and app.post_stop and app.post_shutdown
    assert app.bot.token == "123:secret-token"
    assert len(app.handlers[0]) == 1
    await app.post_shutdown(app)


async def test_updates_are_handled_one_at_a_time_in_telegram_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env(monkeypatch, tmp_path, str(MAY_ID))
    settings = Settings()
    upgrade_to_head(db_path_for(settings.DATA_DIR))

    app = build_application(settings)

    # A typed correction must not overtake the voice note still downloading before it.
    assert app.concurrent_updates == 1
    await app.post_shutdown(app)


async def _unauthorized(*_args: object, **_kwargs: object) -> tuple[int, bytes]:
    return 401, b'{"ok":false,"error_code":401,"description":"Unauthorized"}'


@pytest.mark.usefixtures("restore_http_loggers")
def test_a_rejected_bot_token_stops_without_printing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_env(monkeypatch, tmp_path, str(MAY_ID))
    monkeypatch.setattr(HTTPXRequest, "do_request", _unauthorized)
    caplog.set_level(logging.INFO)

    with pytest.raises(SystemExit) as stopped:
        main()

    assert "TELEGRAM_BOT_TOKEN" in str(stopped.value)
    assert "Invalid token" in caplog.text
    printed = capsys.readouterr()
    for output in (str(stopped.value), caplog.text, printed.out, printed.err):
        assert "secret-token" not in output


@pytest.mark.usefixtures("restore_http_loggers")
def test_the_bot_token_is_redacted_from_every_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging("123:secret-token")
    caplog.set_level(logging.INFO)

    try:
        raise ValueError("The token `123:secret-token` was rejected")
    except ValueError:
        logging.getLogger("telegram.ext").exception("retry loop for %s", "123:secret-token")

    assert "retry loop for" in caplog.text and "ValueError" in caplog.text
    assert "secret-token" not in caplog.text
