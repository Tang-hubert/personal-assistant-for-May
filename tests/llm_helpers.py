from collections.abc import Callable
from typing import Any

import httpx
from google import genai
from google.genai import types
from openai import AsyncOpenAI

from src.adapters.llm.deepseek_adapter import DeepSeekDirectAdapter
from src.adapters.llm.gemini_adapter import GeminiDirectAdapter
from src.config import DEEPSEEK_BASE_URL
from src.domain.budget import PromptParts, Turn
from src.domain.models import TaskCapability
from src.domain.ports import LLMRequest, MediaStorePort

DEEPSEEK_MODEL = "deepseek-flash"
GEMINI_MODEL = "gemini-3.8-flash"
Handler = Callable[[httpx.Request], httpx.Response]


def html_200(req: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, text="<html>bad gateway</html>", headers={"content-type": "text/html"}
    )


def truncated_json_200(req: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, content=b'{"id": "c1", "choi', headers={"content-type": "application/json"}
    )


def deepseek_on(handler: Handler, *, strict: bool = False) -> DeepSeekDirectAdapter:
    client = AsyncOpenAI(
        api_key="test",
        base_url=DEEPSEEK_BASE_URL,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        _strict_response_validation=strict,
    )
    return DeepSeekDirectAdapter(client, DEEPSEEK_MODEL)


def gemini_on(
    handler: Handler, media: MediaStorePort, *, model: str = GEMINI_MODEL
) -> GeminiDirectAdapter:
    client = genai.Client(
        api_key="test",
        http_options=types.HttpOptions(
            async_client_args={"transport": httpx.MockTransport(handler)}
        ),
    )
    return GeminiDirectAdapter(client, model, media)


def request(
    capability: TaskCapability = TaskCapability.TEXT_FAST_ROUTE,
    *,
    tenant_id: str = "t-1",
    user_id: str = "u-1",
    **overrides: Any,
) -> LLMRequest:
    fields: dict[str, Any] = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "workflow_name": "test",
        "capability": capability,
        "prompt": PromptParts(
            system="你是小幫手",
            state_json={"workflow": "sales_copy"},
            retrieved_context="上次的文案",
            recent_turns=(Turn(role="user", text="嗨"), Turn(role="assistant", text="您好")),
            current_user_msg="幫我改親切一點",
        ),
        "max_output_tokens": 256,
    }
    fields.update(overrides)
    return LLMRequest(**fields)
