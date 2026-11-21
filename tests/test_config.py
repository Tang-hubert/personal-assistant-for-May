from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.config import Settings, price_for


def _settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "GEMINI_API_KEY": "test-gemini",
        "GEMINI_MODEL": "gemini-3.8-flash",
        "DEEPSEEK_API_KEY": "test-deepseek",
        "DEEPSEEK_MODEL": "deepseek-flash",
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_ALLOWED_USER_IDS": "111, 222",
        "DISCORD_OPS_WEBHOOK_URL": "https://discord.invalid/webhook",
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)


def test_settings_parse_phase1_env() -> None:
    settings = _settings()
    assert settings.TELEGRAM_ALLOWED_USER_IDS == (111, 222)
    assert settings.DEFAULT_DAILY_BUDGET_USD == Decimal("0.20")
    assert settings.DATA_DIR == Path("data")
    assert "test-gemini" not in repr(settings)


def test_settings_read_comma_separated_ids_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "333,444")
    fields = {
        "GEMINI_API_KEY": "g",
        "GEMINI_MODEL": "gemini-3.8-flash",
        "DEEPSEEK_API_KEY": "d",
        "DEEPSEEK_MODEL": "deepseek-flash",
        "TELEGRAM_BOT_TOKEN": "t",
        "DISCORD_OPS_WEBHOOK_URL": "https://discord.invalid/webhook",
    }
    assert Settings(_env_file=None, **fields).TELEGRAM_ALLOWED_USER_IDS == (333, 444)


@pytest.mark.parametrize("field", ["GEMINI_MODEL", "DEEPSEEK_MODEL"])
def test_unknown_model_fails_fast(field: str) -> None:
    with pytest.raises(ValidationError, match="no price entry"):
        _settings(**{field: "mystery-model-9"})


def test_gemini_promo_price_switches_to_standard_on_2027_01_01() -> None:
    promo = price_for("gemini-3.8-flash", date(2026, 12, 31))
    standard = price_for("gemini-3.8-flash", date(2027, 1, 1))
    assert (promo.input_per_1m, promo.output_per_1m) == (Decimal("0.75"), Decimal("3.75"))
    assert (standard.input_per_1m, standard.output_per_1m) == (Decimal("1.50"), Decimal("7.50"))


def test_deepseek_uses_conservative_peak_price() -> None:
    price = price_for("deepseek-flash", date(2026, 11, 21))
    assert (price.input_per_1m, price.output_per_1m) == (Decimal("0.30"), Decimal("1.20"))


def test_price_for_unknown_model_raises() -> None:
    with pytest.raises(KeyError):
        price_for("mystery-model-9", date(2026, 11, 21))


def test_photo_gate_defaults() -> None:
    settings = _settings()
    assert settings.BLUR_THRESHOLD == 100.0
    assert settings.BATCH_DEBOUNCE_SECONDS == 5.0


def test_webhook_url_whitespace_is_stripped_at_load() -> None:
    settings = _settings(DISCORD_OPS_WEBHOOK_URL="https://discord.invalid/webhook\r\n")
    assert settings.DISCORD_OPS_WEBHOOK_URL.get_secret_value() == "https://discord.invalid/webhook"


@pytest.mark.parametrize("url", ["http://discord.invalid/webhook", "not a url", "https:///path"])
def test_webhook_url_must_be_https_with_a_host(url: str) -> None:
    with pytest.raises(ValidationError, match="DISCORD_OPS_WEBHOOK_URL") as caught:
        _settings(DISCORD_OPS_WEBHOOK_URL=url)
    assert url not in str(caught.value)
