import base64
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from src.adapters.llm.gemini_adapter import GeminiDirectAdapter
from src.adapters.storage.media_repository import SqlMediaStore
from src.domain.models import TaskCapability
from src.domain.ports import LLMUnavailable
from tests.fakes import Owner
from tests.llm_helpers import GEMINI_MODEL as MODEL
from tests.llm_helpers import gemini_on, html_200, request, truncated_json_200


def _ok(usage: dict[str, int] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": "整理好了"}]}}],
            "usageMetadata": usage
            or {"promptTokenCount": 2000, "candidatesTokenCount": 300, "thoughtsTokenCount": 120},
        },
    )


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response], media: SqlMediaStore
) -> GeminiDirectAdapter:
    return gemini_on(handler, media)


def _capture() -> tuple[list[dict[str, Any]], Callable[[httpx.Request], httpx.Response]]:
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith(f"/models/{MODEL}:generateContent")
        bodies.append(json.loads(req.content))
        return _ok()

    return bodies, handler


async def _save(store: SqlMediaStore, owner: Owner, data: bytes, ext: str) -> str:
    return await store.save_asset(
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        data=data,
        media_type="audio" if ext == "ogg" else "image",
        ext=ext,
    )


async def test_text_and_three_images_reach_the_wire_inline_at_medium_resolution(
    media_store: SqlMediaStore, owner: Owner
) -> None:
    images = [f"jpeg-{i}".encode() for i in range(3)]
    asset_ids = tuple([await _save(media_store, owner, data, "jpg") for data in images])
    bodies, handler = _capture()

    await _adapter(handler, media_store).generate(
        request(
            TaskCapability.MULTIMODAL_IMAGE,
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            asset_ids=asset_ids,
        )
    )

    body = bodies[0]
    last = body["contents"][-1]
    assert last["role"] == "user"
    inline = [part["inlineData"] for part in last["parts"] if "inlineData" in part]
    assert [(d["mime_type"], base64.b64decode(d["data"])) for d in inline] == [
        ("image/jpeg", data) for data in images
    ]
    assert last["parts"][-1]["text"] == "幫我改親切一點"
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    system = body["systemInstruction"]["parts"][0]["text"]
    assert system.startswith("你是小幫手")
    assert "上次的文案" in system
    assert body["generationConfig"]["mediaResolution"] == "MEDIA_RESOLUTION_MEDIUM"
    assert body["generationConfig"]["maxOutputTokens"] == 256


async def test_voice_is_uploaded_as_native_ogg(media_store: SqlMediaStore, owner: Owner) -> None:
    voice = await _save(media_store, owner, b"OggS-voice", "ogg")
    bodies, handler = _capture()

    await _adapter(handler, media_store).generate(
        request(
            TaskCapability.MULTIMODAL_AUDIO,
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            asset_ids=(voice,),
            audio_duration_s=42.0,
        )
    )

    inline = bodies[0]["contents"][-1]["parts"][0]["inlineData"]
    assert inline["mime_type"] == "audio/ogg"
    assert base64.b64decode(inline["data"]) == b"OggS-voice"


async def test_json_output_sets_the_response_mime_type(
    media_store: SqlMediaStore, owner: Owner
) -> None:
    bodies, handler = _capture()
    await _adapter(handler, media_store).generate(
        request(TaskCapability.TEXT_FAST_ROUTE, json_output=True)
    )
    assert bodies[0]["generationConfig"]["responseMimeType"] == "application/json"


async def test_completion_tokens_include_thinking_tokens(
    media_store: SqlMediaStore, owner: Owner
) -> None:
    _, handler = _capture()
    result = await _adapter(handler, media_store).generate(request(TaskCapability.COPYWRITING_TW))

    assert result.text == "整理好了"
    assert (result.provider, result.model) == ("gemini", MODEL)
    assert (result.prompt_tokens, result.completion_tokens) == (2000, 420)


async def test_missing_asset_sends_nothing(media_store: SqlMediaStore, owner: Owner) -> None:
    bodies, handler = _capture()
    with pytest.raises(LookupError):
        await _adapter(handler, media_store).generate(
            request(
                TaskCapability.MULTIMODAL_IMAGE,
                tenant_id=owner.tenant_id,
                user_id=owner.user_id,
                asset_ids=("missing",),
            )
        )
    assert bodies == []


def _error(status: int, message: str, code_name: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"error": {"code": status, "message": message, "status": code_name}}
        )

    return handler


@pytest.mark.parametrize(
    ("status", "message", "reason"),
    [
        (402, "Payment required", "balance_exhausted"),
        (
            429,
            "Your prepayment credits are depleted. Please go to AI Studio to manage billing.",
            "balance_exhausted",
        ),
        (429, "Resource has been exhausted (e.g. check quota).", "rate_limited"),
        (503, "The model is overloaded.", "provider_error"),
    ],
)
async def test_api_errors_are_classified(
    media_store: SqlMediaStore, status: int, message: str, reason: str
) -> None:
    handler = _error(status, message, "RESOURCE_EXHAUSTED")
    with pytest.raises(LLMUnavailable) as caught:
        await _adapter(handler, media_store).generate(request(TaskCapability.COPYWRITING_TW))
    assert (caught.value.reason, caught.value.provider) == (reason, "gemini")


async def test_connection_error_becomes_provider_error(media_store: SqlMediaStore) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    with pytest.raises(LLMUnavailable) as caught:
        await _adapter(handler, media_store).generate(request(TaskCapability.COPYWRITING_TW))
    assert caught.value.reason == "provider_error"


@pytest.mark.parametrize("handler", [html_200, truncated_json_200], ids=["html", "truncated"])
async def test_malformed_200_becomes_provider_error(
    media_store: SqlMediaStore, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    with pytest.raises(LLMUnavailable) as caught:
        await _adapter(handler, media_store).generate(request(TaskCapability.COPYWRITING_TW))
    assert (caught.value.reason, caught.value.provider) == ("provider_error", "gemini")


def _no_usage(req: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "好"}]}}]}
    )


async def test_missing_usage_bills_every_image_at_its_budget_rate(
    media_store: SqlMediaStore, owner: Owner
) -> None:
    images = tuple([await _save(media_store, owner, b"jpeg", "jpg") for _ in range(6)])
    req = request(
        TaskCapability.MULTIMODAL_IMAGE,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        asset_ids=images,
    )

    result = await _adapter(_no_usage, media_store).generate(req)

    assert result.prompt_tokens == req.prompt.text_tokens() + 6 * 560


async def test_missing_usage_bills_voice_seconds_and_the_other_assets_as_images(
    media_store: SqlMediaStore, owner: Owner
) -> None:
    voice = await _save(media_store, owner, b"OggS", "ogg")
    image = await _save(media_store, owner, b"jpeg", "jpg")
    req = request(
        TaskCapability.MULTIMODAL_AUDIO,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        asset_ids=(voice, image),
        audio_duration_s=30.0,
    )

    result = await _adapter(_no_usage, media_store).generate(req)

    assert result.prompt_tokens == req.prompt.text_tokens() + 560 + 30 * 32


def _blocked_prompt(req: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [],
            "promptFeedback": {"blockReason": "SAFETY"},
            "usageMetadata": {"promptTokenCount": 900},
        },
    )


async def test_a_reply_without_candidates_is_a_billed_provider_error(
    media_store: SqlMediaStore,
) -> None:
    with pytest.raises(LLMUnavailable) as caught:
        await _adapter(_blocked_prompt, media_store).generate(
            request(TaskCapability.COPYWRITING_TW)
        )
    assert (caught.value.reason, caught.value.provider) == ("provider_error", "gemini")
    assert caught.value.billed is not None
    assert (caught.value.billed.prompt_tokens, caught.value.billed.completion_tokens) == (900, 0)
