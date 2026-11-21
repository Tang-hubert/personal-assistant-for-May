"""Phase 1 settings and the per-model price table (ARCHITECTURE §3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


@dataclass(frozen=True)
class PriceTier:
    effective_from: date
    input_per_1m: Decimal
    output_per_1m: Decimal


def _tier(effective_from: date, input_per_1m: str, output_per_1m: str) -> PriceTier:
    return PriceTier(effective_from, Decimal(input_per_1m), Decimal(output_per_1m))


# Fixed provider endpoint, deliberately not an env var: a typo there would silently
# send keys elsewhere.
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# USD per 1M tokens. Tiers are ordered by effective_from; the Taipei date picks the tier.
MODEL_PRICES: dict[str, tuple[PriceTier, ...]] = {
    "gemini-3.8-flash": (
        _tier(date.min, "0.75", "3.75"),  # official promo price through 2026-12-31
        _tier(date(2027, 1, 1), "1.50", "7.50"),  # standard price
    ),
    "gemini-3.5-flash-lite": (_tier(date.min, "0.30", "2.50"),),  # spare
    # DeepSeek-V4.1-Flash-1017 (replaces the retired deepseek-chat). Billed at the
    # Taipei-daytime peak rate; off-peak is half price, but FinOps would rather
    # over-estimate. T3 must disable thinking mode for TEXT_FAST_ROUTE calls.
    "deepseek-flash": (_tier(date.min, "0.30", "1.20"),),
}


def price_for(model: str, on: date) -> PriceTier:
    """Return the tier in effect on the given Taipei date. Raises KeyError for unknown models."""
    tiers = MODEL_PRICES[model]
    return [tier for tier in tiers if tier.effective_from <= on][-1]


class Settings(BaseSettings):
    # hide_input_in_errors: a startup error must not print the keys or the webhook URL.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    APP_ENV: Literal["dev", "prod", "test"] = "dev"
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL: str
    DEEPSEEK_API_KEY: SecretStr
    DEEPSEEK_MODEL: str
    TELEGRAM_BOT_TOKEN: SecretStr
    TELEGRAM_ALLOWED_USER_IDS: Annotated[tuple[int, ...], NoDecode] = ()
    DISCORD_OPS_WEBHOOK_URL: SecretStr
    DEFAULT_DAILY_BUDGET_USD: Decimal = Decimal("0.20")
    DATA_DIR: Path = Path("data")
    BLUR_THRESHOLD: float = 100.0
    BATCH_DEBOUNCE_SECONDS: float = 5.0

    @field_validator("TELEGRAM_ALLOWED_USER_IDS", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(int(part) for part in value.split(",") if part.strip())
        return value

    @field_validator("DISCORD_OPS_WEBHOOK_URL", mode="before")
    @classmethod
    def _strip_webhook(cls, value: object) -> object:
        # A .env saved on Windows leaves a trailing CR, which httpx rejects at send time.
        return value.strip() if isinstance(value, str) else value

    @field_validator("DISCORD_OPS_WEBHOOK_URL")
    @classmethod
    def _webhook_must_be_https(cls, value: SecretStr) -> SecretStr:
        url = urlsplit(value.get_secret_value())
        if url.scheme != "https" or not url.hostname:
            raise ValueError("DISCORD_OPS_WEBHOOK_URL must be an https URL with a host")
        return value

    @model_validator(mode="after")
    def _models_must_be_priced(self) -> Self:
        for field in ("GEMINI_MODEL", "DEEPSEEK_MODEL"):
            model = getattr(self, field)
            if model not in MODEL_PRICES:
                raise ValueError(f"{field}={model!r} has no price entry in MODEL_PRICES")
        return self
