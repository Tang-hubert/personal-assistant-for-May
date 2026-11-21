"""iOS helper: a phone question or a screenshot (📱) becomes exactly three plain steps. Each guide
is saved as an ios_guide item, so a follow-up sees the last guide as context, not chat history."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.domain import replies
from src.domain.budget import SLOT_RETRIEVED, PromptParts, truncate_to_tokens
from src.domain.media import IntentSelected
from src.domain.models import BotResponse, IncomingEvent, NewKnowledgeItem
from src.domain.models import TaskCapability as Cap
from src.domain.ports import LLMPort, LLMRequest, SessionStorePort, StoragePort
from src.domain.router import HandlerReply, TurnContext
from src.workflows.common import (
    BULLET_MAX_CHARS,
    TW_STYLE,
    BatchCompleter,
    clip,
    open_followup,
    parse_output,
    session_artifact,
)

WORKFLOW = "ios_helper"
SCREENSHOT_WORKFLOW = "ios_screenshot"
MAX_SCREENSHOTS = 2
STEPS = 3
MAX_OUTPUT = 400
SCREENSHOT_HINT = "不是這個的話 直接跟我說想做什麼"

_SCHEMA = (
    '只輸出 JSON：{"title":"15字內的標題","steps":["第一步","第二步","第三步"]}。'
    "剛好 3 步，每步一句白話、25 字內，按鈕名稱照 iPhone 繁體中文畫面上的字寫。"
)
IOS_SYSTEM = TW_STYLE + "你在教一位長輩用 iPhone，要很有耐心、講得很簡單。" + _SCHEMA
FOLLOWUP_SYSTEM = (
    TW_STYLE + "你在教一位長輩用 iPhone。[context] 是上一份教學，依她這次的問題重新教。" + _SCHEMA
)
SCREENSHOT_SYSTEM = (
    TW_STYLE + "長輩傳了 iPhone 的截圖。猜她最可能想在這個畫面做什麼，教她怎麼做。" + _SCHEMA
)


class GuideOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    steps: tuple[str, ...] = Field(min_length=STEPS)


def _steps(out: GuideOut) -> tuple[str, ...]:
    return tuple(
        clip(f"第{n}步：{step}", BULLET_MAX_CHARS) for n, step in enumerate(out.steps[:STEPS], 1)
    )


def guide_response(title: str, steps: tuple[str, ...]) -> BotResponse:
    return BotResponse(title=clip(title, 30), bullets=steps, actions=(replies.finish_button(),))


class IosHelper:
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

    async def handle(self, ctx: TurnContext) -> HandlerReply:
        tenant_id, user_id = ctx.event.tenant_id, ctx.event.user_id
        previous = await session_artifact(
            self._storage, tenant_id=tenant_id, user_id=user_id, session=ctx.session
        )
        if previous is not None and previous.category == "ios_guide":
            prompt = PromptParts(
                system=FOLLOWUP_SYSTEM,
                state_json=ctx.session.state_json,
                retrieved_context=truncate_to_tokens(previous.content_text, SLOT_RETRIEVED),
                recent_turns=ctx.session.recent_turns,
                current_user_msg=ctx.user_text,
            )
        else:
            prompt = PromptParts(system=IOS_SYSTEM, current_user_msg=ctx.user_text)
        result = await self._llm.generate(
            LLMRequest(
                tenant_id=tenant_id,
                user_id=user_id,
                workflow_name=WORKFLOW,
                capability=Cap.COPYWRITING_TW,
                prompt=prompt,
                max_output_tokens=MAX_OUTPUT,
                json_output=True,
            )
        )
        out = parse_output(result, GuideOut)
        steps = _steps(out)
        artifact_id = await self._save(tenant_id, user_id, out.title, steps, None)
        return HandlerReply(
            text="\n".join(steps),
            response=guide_response(out.title, steps),
            last_artifact_id=artifact_id,
        )

    async def draft(self, event: IncomingEvent, selected: IntentSelected) -> BotResponse:
        tenant_id, user_id = event.tenant_id, event.user_id
        assets = selected.kept_asset_ids[:MAX_SCREENSHOTS]
        result = await self._llm.generate(
            LLMRequest(
                tenant_id=tenant_id,
                user_id=user_id,
                workflow_name=SCREENSHOT_WORKFLOW,
                capability=Cap.MULTIMODAL_IMAGE,
                prompt=PromptParts(system=SCREENSHOT_SYSTEM, current_user_msg="這個畫面要怎麼用"),
                asset_ids=assets,
                max_output_tokens=MAX_OUTPUT,
                json_output=True,
            )
        )
        out = parse_output(result, GuideOut)
        steps = _steps(out)
        artifact_id = await self._save(
            tenant_id, user_id, out.title, steps, assets[0] if assets else None
        )
        await open_followup(
            self._sessions,
            tenant_id=tenant_id,
            user_id=user_id,
            topic_type="ios_help",
            workflow=SCREENSHOT_WORKFLOW,
            artifact_id=artifact_id,
        )
        await self._batches.complete(
            tenant_id=tenant_id, user_id=user_id, batch_id=selected.batch_id
        )
        hinted = (*steps[:-1], f"{steps[-1]}\n{SCREENSHOT_HINT}")
        return guide_response(out.title, hinted)

    async def _save(
        self,
        tenant_id: str,
        user_id: str,
        title: str,
        steps: tuple[str, ...],
        primary_asset_id: str | None,
    ) -> str:
        return await self._storage.save_knowledge(
            tenant_id=tenant_id,
            user_id=user_id,
            item=NewKnowledgeItem(
                category="ios_guide",
                title=clip(title, 30),
                content_text="\n".join((title, *steps)),
                structured_json={"title": title, "steps": list(steps)},
                primary_asset_id=primary_asset_id,
            ),
        )
