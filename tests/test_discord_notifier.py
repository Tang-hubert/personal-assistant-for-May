import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from src.adapters.discord_ops.notifier import DiscordWebhookNotifier

WEBHOOK = "https://discord.invalid/api/webhooks/1/secret"


def _notifier(handler: httpx.MockTransport | None = None) -> DiscordWebhookNotifier:
    client = httpx.AsyncClient(transport=handler) if handler else httpx.AsyncClient()
    return DiscordWebhookNotifier(SecretStr(WEBHOOK), client)


async def test_posts_level_tagged_content_to_the_webhook() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    await _notifier(httpx.MockTransport(handler)).notify("critical", "gemini 餘額用完")

    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert str(seen[0].url) == WEBHOOK
    assert json.loads(seen[0].content) == {"content": "[CRITICAL] gemini 餘額用完"}


async def test_content_is_capped_at_discord_limit() -> None:
    bodies: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(204)

    await _notifier(httpx.MockTransport(handler)).notify("warning", "x" * 5000)

    assert len(bodies[0]["content"]) == 2000


async def test_discord_failure_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with caplog.at_level(logging.ERROR):
        await _notifier(httpx.MockTransport(handler)).notify("warning", "hello")

    assert "discord" in caplog.text.lower()
    assert WEBHOOK not in caplog.text


async def test_unmocked_webhook_hits_the_network_guard() -> None:
    with pytest.raises(RuntimeError, match="Network blocked"):
        await _notifier().notify("error", "hello")


async def test_an_unparseable_webhook_url_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = DiscordWebhookNotifier(SecretStr(WEBHOOK + "\r"), client)
    with caplog.at_level(logging.ERROR):
        await notifier.notify("warning", "hello")

    assert "discord" in caplog.text.lower()
    assert "secret" not in caplog.text
