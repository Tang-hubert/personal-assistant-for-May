"""Discord Ops webhook (OpsNotifierPort). An alert that fails to send must never cost May
her reply, so any send failure is logged and swallowed."""

from __future__ import annotations

import logging
from typing import Literal

import httpx
from pydantic import SecretStr

logger = logging.getLogger(__name__)

DISCORD_CONTENT_MAX_CHARS = 2000
TIMEOUT_S = 10.0


class DiscordWebhookNotifier:
    def __init__(self, webhook_url: SecretStr, client: httpx.AsyncClient) -> None:
        self._webhook_url = webhook_url
        self._client = client

    async def notify(self, level: Literal["warning", "critical", "error"], message: str) -> None:
        content = f"[{level.upper()}] {message}"[:DISCORD_CONTENT_MAX_CHARS]
        try:
            response = await self._client.post(
                self._webhook_url.get_secret_value(),
                json={"content": content},
                timeout=TIMEOUT_S,
            )
            response.raise_for_status()
        except RuntimeError:
            # The test network guard raises RuntimeError; an unmocked send must fail loudly.
            raise
        except Exception as err:
            # str(err) embeds the webhook URL, which is itself the credential.
            status = err.response.status_code if isinstance(err, httpx.HTTPStatusError) else None
            logger.error("discord ops alert not sent: %s status=%s", type(err).__name__, status)
