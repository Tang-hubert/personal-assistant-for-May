"""Gemini direct adapter (google-genai SDK): text, up to 6 images, and native audio inline."""

from __future__ import annotations

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

TIMEOUT_MS = 60_000
# 402 and 429 are never retried: an empty prepaid balance does not refill within seconds.
RETRY = types.HttpRetryOptions(attempts=3, http_status_codes=[408, 500, 502, 503, 504])


def create_gemini_client(api_key: SecretStr) -> genai.Client:
    return genai.Client(
        api_key=api_key.get_secret_value(),
        http_options=types.HttpOptions(timeout=TIMEOUT_MS, retry_options=RETRY),
    )


def _classify(err: errors.APIError) -> UnavailableReason:
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
            max_output_tokens=request.max_output_tokens,
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
        except errors.APIError as err:
            raise LLMUnavailable(_classify(err), "gemini") from err
        except (
            httpx.HTTPError,
            httpx2.HTTPError,
            ValueError,
            AttributeError,
            IndexError,
            KeyError,
            TypeError,
        ) as err:
            raise LLMUnavailable("provider_error", "gemini") from err

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
        )
        if not answered:
            # A blocked prompt comes back with no candidates, yet its input was still billed.
            raise LLMUnavailable("provider_error", "gemini", result if usage else None)
        return result
