"""DeepSeek direct adapter (openai SDK, Chat Completions). Text only: TEXT_FAST_ROUTE and the
COPYWRITING_TW fallback."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import openai
from openai import AsyncOpenAI
from pydantic import SecretStr

from src.config import DEEPSEEK_BASE_URL
from src.domain.budget import estimate_tokens
from src.domain.ports import LLMRequest, LLMResult, LLMUnavailable, UnavailableReason

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

# DeepSeek turns thinking mode on by default; it would bill reasoning tokens on every routing
# call and slow it down. Sent on every request, never left to the server default.
THINKING_DISABLED: dict[str, Any] = {"thinking": {"type": "disabled"}}


def create_deepseek_client(api_key: SecretStr) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=api_key.get_secret_value(), base_url=DEEPSEEK_BASE_URL, max_retries=1, timeout=30
    )


def _reason_for_status(status: int) -> UnavailableReason:
    if status == 402:
        return "balance_exhausted"
    if status == 429:
        return "rate_limited"
    return "provider_error"


class DeepSeekDirectAdapter:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def generate(self, request: LLMRequest) -> LLMResult:
        if request.asset_ids or request.audio_duration_s is not None:
            raise ValueError("DeepSeek is text only; media requests must go to Gemini")
        parts = request.prompt
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": parts.system_text()}
        ]
        for turn in parts.recent_turns:
            if turn.role == "user":
                messages.append({"role": "user", "content": turn.text})
            else:
                messages.append({"role": "assistant", "content": turn.text})
        messages.append({"role": "user", "content": parts.current_user_msg})

        extra: dict[str, Any] = {}
        if request.json_output:
            extra["response_format"] = {"type": "json_object"}
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=request.max_output_tokens,
                extra_body=THINKING_DISABLED,
                **extra,
            )
        except openai.APIStatusError as err:
            raise LLMUnavailable(_reason_for_status(err.status_code), "deepseek") from err
        except (openai.APIError, ValueError) as err:
            raise LLMUnavailable("provider_error", "deepseek") from err

        try:
            # A non-JSON 200 body comes back from the SDK as a bare str.
            usage = response.usage
        except AttributeError as err:
            raise LLMUnavailable("provider_error", "deepseek") from err
        try:
            text = response.choices[0].message.content or ""
        except (AttributeError, IndexError, KeyError, TypeError) as err:
            billed = (
                None
                if usage is None
                else self._result("", usage.prompt_tokens, usage.completion_tokens)
            )
            raise LLMUnavailable("provider_error", "deepseek", billed) from err
        # FinOps would rather over-count: without usage, fall back to the estimator.
        if usage is None:
            return self._result(text, parts.text_tokens(), estimate_tokens(text))
        return self._result(text, usage.prompt_tokens, usage.completion_tokens)

    def _result(self, text: str, prompt_tokens: int, completion_tokens: int) -> LLMResult:
        return LLMResult(
            text=text,
            provider="deepseek",
            # The configured ID, not response.model: it is the key into MODEL_PRICES.
            model=self._model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
