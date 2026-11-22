"""Lecture digest: kept batch photos -> 3 plain takeaways + purpose / reminder (ARCHITECTURE §4.3
Layer 1), then a clean Layer-2 session for follow-up questions."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from src.domain import replies
from src.domain.budget import SLOT_RETRIEVED, PromptParts, truncate_to_tokens
from src.domain.media import MAX_BATCH_IMAGES, IntentSelected
from src.domain.models import BotResponse, CopyableBlock, IncomingEvent, NewKnowledgeItem
from src.domain.models import TaskCapability as Cap
from src.domain.ports import LLMPort, LLMRequest, SessionStorePort, StoragePort
from src.domain.router import HandlerReply, TurnContext
from src.workflows.common import (
    BLOCK_MAX_CHARS,
    BULLET_MAX_CHARS,
    TW_STYLE,
    BatchCompleter,
    ClippedTitle,
    clip,
    open_followup,
    parse_output,
    session_artifact,
    text_list,
)

WORKFLOW = "lecture_digest"
FOLLOWUP_WORKFLOW = "lecture_followup"
MAX_OUTPUT = 800
FOLLOWUP_MAX_OUTPUT = 400

LECTURE_SYSTEM = TW_STYLE + (
    "你在幫一位長輩整理講座的照片（投影片或筆記）。只輸出 JSON："
    '{"title":"15字內的講座主題","takeaways":["重點1","重點2","重點3"],'
    '"purpose":"這場講座想讓聽眾做什麼","reminder":"給長輩的溫馨提醒"}。'
    "重點剛好 3 個，每個一句白話、30 字內；看不清楚的內容不要亂猜。"
)
FOLLOWUP_SYSTEM = TW_STYLE + (
    "[context] 是之前整理好的講座筆記。根據筆記回答長輩的追問，筆記沒寫的就老實說不知道。"
    '只輸出 JSON：{"bullets":["最多 3 句白話回答"]}。'
)


_Bullets = Annotated[tuple[str, ...], BeforeValidator(text_list(3, BULLET_MAX_CHARS))]


class LectureOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: ClippedTitle = Field(min_length=1)
    takeaways: _Bullets = Field(min_length=1)
    purpose: str = ""
    reminder: str = ""


class FollowupOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    bullets: _Bullets = Field(min_length=1)


def _content(out: LectureOut) -> str:
    lines = [out.title, *(f"{i}. {t}" for i, t in enumerate(out.takeaways[:3], 1))]
    lines += [f"目的：{out.purpose}", f"溫馨提醒：{out.reminder}"]
    return "\n".join(lines)


class LectureDigest:
    def __init__(
        self,
        *,
        llm: LLMPort,
        storage: StoragePort,
        sessions: SessionStorePort,
        batches: BatchCompleter,
    ) -> None:
        self._llm = llm
        self._storage = storage
        self._sessions = sessions
        self._batches = batches

    async def draft(self, event: IncomingEvent, selected: IntentSelected) -> BotResponse:
        tenant_id, user_id = event.tenant_id, event.user_id
        assets = selected.kept_asset_ids[:MAX_BATCH_IMAGES]
        result = await self._llm.generate(
            LLMRequest(
                tenant_id=tenant_id,
                user_id=user_id,
                workflow_name=WORKFLOW,
                capability=Cap.MULTIMODAL_IMAGE,
                prompt=PromptParts(system=LECTURE_SYSTEM, current_user_msg="請幫我整理這場講座"),
                asset_ids=assets,
                max_output_tokens=MAX_OUTPUT,
                json_output=True,
            )
        )
        out = parse_output(result, LectureOut, workflow=WORKFLOW)
        title = clip(out.title, 30)
        artifact_id = await self._storage.save_knowledge(
            tenant_id=tenant_id,
            user_id=user_id,
            item=NewKnowledgeItem(
                category="lecture",
                title=title,
                content_text=_content(out),
                structured_json=out.model_dump(mode="json"),
                primary_asset_id=assets[0] if assets else None,
            ),
        )
        response = BotResponse(
            title=title,
            bullets=tuple(clip(t, BULLET_MAX_CHARS) for t in out.takeaways[:3]),
            copyable_blocks=(
                CopyableBlock(
                    label="這場講座的目的／溫馨提醒",
                    body_text=clip(
                        f"目的：{out.purpose}\n溫馨提醒：{out.reminder}", BLOCK_MAX_CHARS
                    ),
                ),
            ),
            actions=(replies.finish_button(),),
        )
        await open_followup(
            self._sessions,
            tenant_id=tenant_id,
            user_id=user_id,
            topic_type="lecture",
            workflow=WORKFLOW,
            artifact_id=artifact_id,
        )
        await self._batches.complete(
            tenant_id=tenant_id,
            user_id=user_id,
            batch_id=selected.batch_id,
            intent=selected.intent,
        )
        return response

    async def follow_up(self, ctx: TurnContext) -> HandlerReply:
        tenant_id, user_id = ctx.event.tenant_id, ctx.event.user_id
        item = await session_artifact(
            self._storage, tenant_id=tenant_id, user_id=user_id, session=ctx.session
        )
        if item is None:
            return HandlerReply(text="請先傳講座照片", response=send_photos_first())
        result = await self._llm.generate(
            LLMRequest(
                tenant_id=tenant_id,
                user_id=user_id,
                workflow_name=FOLLOWUP_WORKFLOW,
                capability=Cap.COPYWRITING_TW,
                prompt=PromptParts(
                    system=FOLLOWUP_SYSTEM,
                    state_json=ctx.session.state_json,
                    retrieved_context=truncate_to_tokens(item.content_text, SLOT_RETRIEVED),
                    recent_turns=ctx.session.recent_turns,
                    current_user_msg=ctx.user_text,
                ),
                max_output_tokens=FOLLOWUP_MAX_OUTPUT,
                json_output=True,
            )
        )
        bullets = tuple(
            clip(b, BULLET_MAX_CHARS)
            for b in parse_output(result, FollowupOut, workflow=FOLLOWUP_WORKFLOW).bullets[:3]
        )
        return HandlerReply(
            text="\n".join(bullets),
            response=BotResponse(
                title=clip(item.title, 30), bullets=bullets, actions=(replies.finish_button(),)
            ),
        )


def send_photos_first() -> BotResponse:
    return BotResponse(
        title="先傳講座照片給我",
        bullets=("請把投影片或筆記拍下來傳給我", "再按「📚 整理講座重點」"),
    )
