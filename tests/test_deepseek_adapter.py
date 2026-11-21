import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from src.adapters.llm.deepseek_adapter import DeepSeekDirectAdapter, create_deepseek_client
from src.config import DEEPSEEK_BASE_URL
from src.domain.models import TaskCapability
from src.domain.ports import LLMUnavailable
from tests.llm_helpers import DEEPSEEK_MODEL as MODEL
from tests.llm_helpers import deepseek_on, html_200, request, truncated_json_200


def _completion(text: str = "好的", usage: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-v4.1-flash-1017",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": usage or {"prompt_tokens": 40, "completion_tokens": 7, "total_tokens": 47},
    }


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> DeepSeekDirectAdapter:
    return deepseek_on(handler)


def _capture() -> tuple[list[httpx.Request], Callable[[httpx.Request], httpx.Response]]:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_completion())

    return seen, handler


async def test_wire_request_disables_thinking_and_targets_deepseek() -> None:
    seen, handler = _capture()
    await _adapter(handler).generate(request())

    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.deepseek.com/chat/completions"
    body = json.loads(seen[0].content)
    assert body["thinking"] == {"type": "disabled"}
    assert body["model"] == MODEL
    assert body["max_tokens"] == 256
    assert "response_format" not in body


async def test_wire_messages_carry_system_slots_turns_and_user_message() -> None:
    seen, handler = _capture()
    await _adapter(handler).generate(request())

    messages = json.loads(seen[0].content)["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[0]["content"].startswith("你是小幫手")
    assert '{"workflow":"sales_copy"}' in messages[0]["content"]
    assert "上次的文案" in messages[0]["content"]
    assert [m["content"] for m in messages[1:]] == ["嗨", "您好", "幫我改親切一點"]


async def test_json_output_requests_a_json_object() -> None:
    seen, handler = _capture()
    await _adapter(handler).generate(request(json_output=True))

    body = json.loads(seen[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}


async def test_usage_maps_to_the_result_with_the_priced_model_id() -> None:
    _, handler = _capture()
    result = await _adapter(handler).generate(request())

    assert result.text == "好的"
    assert result.provider == "deepseek"
    assert result.model == MODEL
    assert (result.prompt_tokens, result.completion_tokens) == (40, 7)


@pytest.mark.parametrize(
    ("status", "reason"),
    [(402, "balance_exhausted"), (429, "rate_limited"), (500, "provider_error")],
)
async def test_http_errors_become_llm_unavailable(status: int, reason: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope"}})

    with pytest.raises(LLMUnavailable) as caught:
        await _adapter(handler).generate(request())
    assert (caught.value.reason, caught.value.provider) == (reason, "deepseek")


async def test_connection_error_becomes_provider_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    with pytest.raises(LLMUnavailable) as caught:
        await _adapter(handler).generate(request())
    assert caught.value.reason == "provider_error"


async def test_media_requests_are_a_wiring_error_and_send_nothing() -> None:
    seen, handler = _capture()
    with pytest.raises(ValueError, match="text only"):
        await _adapter(handler).generate(
            request(TaskCapability.MULTIMODAL_IMAGE, asset_ids=("a1",))
        )
    assert seen == []


def test_production_client_targets_the_fixed_base_url() -> None:
    client = create_deepseek_client(SecretStr("k"))
    assert str(client.base_url).rstrip("/") == DEEPSEEK_BASE_URL == "https://api.deepseek.com"


USAGE = {"prompt_tokens": 40, "completion_tokens": 7, "total_tokens": 47}


def _no_choices(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={**_completion(), "choices": [], "usage": USAGE})


@pytest.mark.parametrize(
    "handler", [_no_choices, html_200, truncated_json_200], ids=["no_choices", "html", "truncated"]
)
async def test_malformed_200_becomes_provider_error(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    with pytest.raises(LLMUnavailable) as caught:
        await _adapter(handler).generate(request())
    assert (caught.value.reason, caught.value.provider) == ("provider_error", "deepseek")


async def test_response_validation_error_becomes_provider_error() -> None:
    adapter = deepseek_on(lambda req: httpx.Response(200, json={"choices": "x"}), strict=True)
    with pytest.raises(LLMUnavailable) as caught:
        await adapter.generate(request())
    assert caught.value.reason == "provider_error"


async def test_a_billed_200_without_choices_carries_its_usage() -> None:
    with pytest.raises(LLMUnavailable) as caught:
        await _adapter(_no_choices).generate(request())
    billed = caught.value.billed
    assert billed is not None
    assert (billed.provider, billed.model, billed.text) == ("deepseek", MODEL, "")
    assert (billed.prompt_tokens, billed.completion_tokens) == (40, 7)


async def test_an_unbilled_malformed_200_carries_no_usage() -> None:
    with pytest.raises(LLMUnavailable) as caught:
        await _adapter(html_200).generate(request())
    assert caught.value.billed is None
