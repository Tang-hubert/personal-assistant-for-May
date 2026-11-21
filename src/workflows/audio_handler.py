"""Voice transcription, the Router's Transcriber. The transcript then takes the typed-text path,
so a voice message gets the same routing and Layer-2 follow-ups as text."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.domain.budget import PromptParts
from src.domain.models import IncomingEvent
from src.domain.models import TaskCapability as Cap
from src.domain.ports import LLMPort, LLMRequest
from src.workflows.common import TW_STYLE, parse_output

WORKFLOW = "voice_transcribe"
MAX_OUTPUT = 1200

TRANSCRIBE_SYSTEM = TW_STYLE + (
    "你只負責把長輩的語音逐字轉寫成文字，不要回答、不要摘要、不要加任何內容。"
    '只輸出 JSON：{"transcript":"逐字稿","audible":true}。'
    '聽不清楚或沒有人說話時輸出 {"transcript":"","audible":false}。'
)


class TranscriptOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    transcript: str = ""
    audible: bool = True


class VoiceTranscriber:
    def __init__(self, llm: LLMPort) -> None:
        self._llm = llm

    async def __call__(self, event: IncomingEvent) -> str:
        result = await self._llm.generate(
            LLMRequest(
                tenant_id=event.tenant_id,
                user_id=event.user_id,
                workflow_name=WORKFLOW,
                capability=Cap.MULTIMODAL_AUDIO,
                prompt=PromptParts(system=TRANSCRIBE_SYSTEM, current_user_msg="請逐字轉寫這段語音"),
                asset_ids=event.asset_ids[:1],
                audio_duration_s=event.audio_duration_s,
                max_output_tokens=MAX_OUTPUT,
                json_output=True,
            )
        )
        out = parse_output(result, TranscriptOut)
        return out.transcript.strip() if out.audible else ""
