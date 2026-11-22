"""Gemini direct adapter (google-genai SDK): text, up to 6 images, and native audio inline."""

from __future__ import annotations

import logging

import httpx
import httpx2
from google import genai
from google.genai import errors, types
from pydantic import SecretStr

from src.domain.budget import estimate_tokens, multimodal_estimate
from src.domain.ports import (
    LLMRequest,
    LLMResult,
    LLMUnavailable,
    MediaStorePort,
    UnavailableReason,
)

logger = logging.getLogger(__name__)

# Per attempt. One retry only: a 503/504 that survives it goes to the spare model (RoutingLLM).
TIMEOUT_MS = 30_000
# 402 and 429 are never retried: an empty prepaid balance does not refill within seconds.
RETRY = types.HttpRetryOptions(attempts=2, http_status_codes=[408, 500, 502, 503, 504])

# Gemini counts thinking tokens against max_output_tokens. Left at the default (dynamic)
# level, thinking used up the 400-token cap of an iOS guide and cut its JSON mid-way. Each
# model's level is pinned, and its thinking headroom is added on top of the workflow's cap,
# which stays the size of the answer alone.
THINKING_LEVELS: dict[str, types.ThinkingLevel] = {
    "gemini-3.8-flash": types.ThinkingLevel.LOW,
    "gemini-3.5-flash-lite": types.ThinkingLevel.MINIMAL,
}
DEFAULT_THINKING = types.ThinkingLevel.LOW
THINKING_HEADROOM: dict[types.ThinkingLevel, int] = {
    types.ThinkingLevel.MINIMAL: 512,
    types.ThinkingLevel.LOW: 1024,
}


def create_gemini_client(api_key: SecretStr) -> genai.Client:
    return genai.Client(
        api_key=api_key.get_secret_value(),
        http_options=types.HttpOptions(timeout=TIMEOUT_MS, retry_options=RETRY),
    )


def _classify(err: errors.APIError) -> UnavailableReason:
    if err.code in (503, 504):
        return "overloaded"
    if err.code == 402:
        return "balance_exhausted"
    if err.code == 429:
        # Before switching to 402, Google reported an empty prepaid balance as a 429.
        if "prepayment credits" in (err.message or "").lower():
            return "balance_exhausted"
        return "rate_limited"
    return "provider_error"


class GeminiDirectAdapter:
    def __init__(self, client: genai.Client, model: str, media: MediaStorePort) -> None:
        self._client = client
        self._model = model
        self._media = media
        self._thinking = THINKING_LEVELS.get(model, DEFAULT_THINKING)

    async def generate(self, request: LLMRequest) -> LLMResult:
        parts = request.prompt
        media_parts: list[types.Part] = []
        for asset_id in request.asset_ids:
            blob = await self._media.read_asset_blob(
                tenant_id=request.tenant_id, user_id=request.user_id, asset_id=asset_id
            )
            if blob is None:
                raise LookupError(f"asset {asset_id} is not readable for this owner")
            media_parts.append(types.Part.from_bytes(data=blob.data, mime_type=blob.mime_type))

        contents = [
            types.Content(
                role="user" if turn.role == "user" else "model",
                parts=[types.Part.from_text(text=turn.text)],
            )
            for turn in parts.recent_turns
        ]
        contents.append(
            types.Content(
                role="user", parts=[*media_parts, types.Part.from_text(text=parts.current_user_msg)]
            )
        )
        config = types.GenerateContentConfig(
            system_instruction=parts.system_text(),
            max_output_tokens=request.max_output_tokens + THINKING_HEADROOM[self._thinking],
            thinking_config=types.ThinkingConfig(thinking_level=self._thinking),
            # Fixed at 560 tokens per image; the multimodal budget is computed at this rate.
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
            response_mime_type="application/json" if request.json_output else None,
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=contents, config=config
            )
            text = response.text or ""
            usage = response.usage_metadata
            answered = bool(response.candidates)
            finish = response.candidates[0].finish_reason if response.candidates else None
        except errors.APIError as err:
            raise LLMUnavailable(_classify(err), "gemini", detail=f"http {err.code}") from err
        except (
            httpx.HTTPError,
            httpx2.HTTPError,
            ValueError,
            AttributeError,
            IndexError,
            KeyError,
            TypeError,
        ) as err:
            raise LLMUnavailable("provider_error", "gemini", detail=type(err).__name__) from err

        prompt_tokens = usage.prompt_token_count if usage else None
        candidates = usage.candidates_token_count if usage else None
        thoughts = usage.thoughts_token_count if usage else None
        # Thinking tokens bill at the output rate; leaving them out would under-count cost.
        result = LLMResult(
            text=text,
            provider="gemini",
            model=self._model,
            prompt_tokens=prompt_tokens
            if prompt_tokens is not None
            else multimodal_estimate(parts, request.image_count, request.audio_duration_s),
            completion_tokens=(candidates if candidates is not None else estimate_tokens(text))
            + (thoughts or 0),
            finish_reason=finish.name if finish is not None else None,
        )
        if not answered:
            # A blocked prompt comes back with no candidates, yet its input was still billed.
            raise LLMUnavailable(
                "provider_error", "gemini", result if usage else None, detail="no candidates"
            )
        if result.finish_reason not in (None, "STOP"):
            logger.warning(
                "gemini reply not finished: model=%s finish=%s out=%s thoughts=%s",
                self._model,
                result.finish_reason,
                candidates,
                thoughts,
            )
        return result
