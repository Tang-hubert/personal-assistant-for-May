"""The seam the Gemini adapter tests rely on: google-genai's async client sends through an
httpx.MockTransport passed in async_client_args (google-genai picks its legacy-httpx backend
when it sees an httpx transport)."""

import json

import httpx
from google import genai
from google.genai import types


def _gemini_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": "hi"}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
        },
    )


async def test_genai_async_client_routes_through_injected_mock_transport() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _gemini_ok(request)

    client = genai.Client(
        api_key="test",
        http_options=types.HttpOptions(
            async_client_args={"transport": httpx.MockTransport(handler)}
        ),
    )
    result = await client.aio.models.generate_content(model="gemini-3.8-flash", contents="hello")

    assert result.text == "hi"
    assert len(seen) == 1
    assert seen[0].url.path.endswith("/models/gemini-3.8-flash:generateContent")
    assert json.loads(seen[0].content)["contents"][0]["parts"][0]["text"] == "hello"
