"""Dual-track sales copy (ARCHITECTURE §6). Version A always passes the deterministic scanner;
Version B, kept only when it carries a risky claim, ships with a legal warning. The Layer-2
follow-up uses the same dual track, because May usually adds her claims there."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.domain import replies
from src.domain.budget import SLOT_RETRIEVED, PromptParts, truncate_to_tokens
from src.domain.compliance import ComplianceHit, legal_warning, sanitize, scan
from src.domain.media import IntentSelected
from src.domain.models import (
    BotResponse,
    CopyableBlock,
    IncomingEvent,
    KnowledgeCategory,
    NewKnowledgeItem,
)
from src.domain.models import TaskCapability as Cap
from src.domain.ports import LLMPort, LLMRequest, SessionStorePort, StoragePort
from src.domain.router import HandlerReply, TurnContext
from src.workflows.common import (
    BLOCK_MAX_CHARS,
    TW_STYLE,
    BatchCompleter,
    ClippedTitle,
    clip,
    open_followup,
    parse_output,
    session_artifact,
)

WORKFLOW = "sales_copy"
FOLLOWUP_WORKFLOW = "sales_copy_followup"
MAX_SALES_IMAGES = 3
MAX_OUTPUT = 1500

_SCHEMA = (
    '只輸出 JSON：{"category":"jewelry 或 product","title":"15字內標題",'
    '"version_a":"安全版文案","version_b":"原意版文案或 null"}。'
    "version_a 用外觀、工藝、心情、送禮情境打動人，絕不寫療效、治病、保證效果或收入。"
    "version_b 只在長輩想強調功效或收益時才寫，保留她的原意；沒有就給 null。每版 300 字內。"
)
SALES_SYSTEM = TW_STYLE + "你幫一位長輩寫珠寶或商品的社群推銷文案。" + _SCHEMA
FOLLOWUP_SYSTEM = TW_STYLE + "[context] 是目前的安全版文案。依長輩這次的要求改寫。" + _SCHEMA


class SalesOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: Literal["jewelry", "product"] = "jewelry"
    title: ClippedTitle = Field(min_length=1)
    version_a: str = Field(min_length=1)
    version_b: str | None = None


class DualTrack(BaseModel):
    """What May gets: A is clean by construction; B exists only with its risky hits."""

    model_config = ConfigDict(frozen=True)

    version_a: str
    version_b: str | None
    hits_a: tuple[ComplianceHit, ...]
    hits_b: tuple[ComplianceHit, ...]


def dual_track(out: SalesOut) -> DualTrack:
    hits_a = scan(out.version_a)
    version_a = clip(sanitize(out.version_a), BLOCK_MAX_CHARS)
    version_b = clip(out.version_b, BLOCK_MAX_CHARS) if out.version_b else None
    hits_b = scan(version_b) if version_b else ()
    return DualTrack(
        version_a=version_a,
        version_b=version_b if hits_b else None,
        hits_a=hits_a,
        hits_b=hits_b,
    )


def render(title: str, track: DualTrack) -> BotResponse:
    blocks = [CopyableBlock(label="A 版 可以放心貼", body_text=track.version_a)]
    bullets = ["A 版可以放心直接貼"]
    if track.version_b is not None:
        blocks.append(
            CopyableBlock(
                label="B 版 原意（有風險）",
                body_text=track.version_b,
                warning_note=legal_warning(track.hits_b),
            )
        )
        bullets.append("B 版有法律風險，請先看提醒")
    bullets.append("想改語氣，直接跟我說就好")
    return BotResponse(
        title=clip(title, 30),
        bullets=tuple(bullets),
        copyable_blocks=tuple(blocks),
        actions=(replies.finish_button(),),
    )


class SalesCopy:
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
        assets = selected.kept_asset_ids[:MAX_SALES_IMAGES]
        result = await self._llm.generate(
            LLMRequest(
                tenant_id=tenant_id,
                user_id=user_id,
                workflow_name=WORKFLOW,
                capability=Cap.MULTIMODAL_IMAGE,
                prompt=PromptParts(system=SALES_SYSTEM, current_user_msg="請幫我寫這件商品的文案"),
                asset_ids=assets,
                max_output_tokens=MAX_OUTPUT,
                json_output=True,
            )
        )
        out = parse_output(result, SalesOut, workflow=WORKFLOW)
        track = dual_track(out)
        artifact_id = await self._save(
            tenant_id, user_id, out.category, out.title, track, assets[0] if assets else None
        )
        response = render(out.title, track)
        await open_followup(
            self._sessions,
            tenant_id=tenant_id,
            user_id=user_id,
            topic_type="sales",
            workflow=WORKFLOW,
            artifact_id=artifact_id,
        )
        await self._batches.complete(
            tenant_id=tenant_id, user_id=user_id, batch_id=selected.batch_id
        )
        return response

    async def follow_up(self, ctx: TurnContext) -> HandlerReply:
        tenant_id, user_id = ctx.event.tenant_id, ctx.event.user_id
        item = await session_artifact(
            self._storage, tenant_id=tenant_id, user_id=user_id, session=ctx.session
        )
        if item is None:
            return HandlerReply(text="請先傳商品照片", response=send_photos_first())
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
                max_output_tokens=MAX_OUTPUT,
                json_output=True,
            )
        )
        out = parse_output(result, SalesOut, workflow=FOLLOWUP_WORKFLOW)
        track = dual_track(out)
        artifact_id = await self._save(
            tenant_id,
            user_id,
            item.category,
            out.title,
            track,
            item.primary_asset_id,
            revision_of=item.id,
        )
        return HandlerReply(
            text=f"已改寫文案：{track.version_a[:80]}",
            response=render(out.title, track),
            last_artifact_id=artifact_id,
        )

    async def _save(
        self,
        tenant_id: str,
        user_id: str,
        category: KnowledgeCategory,
        title: str,
        track: DualTrack,
        primary_asset_id: str | None,
        revision_of: str | None = None,
    ) -> str:
        structured = track.model_dump(mode="json")
        if revision_of is not None:
            structured["revision_of"] = revision_of
        return await self._storage.save_knowledge(
            tenant_id=tenant_id,
            user_id=user_id,
            item=NewKnowledgeItem(
                category=category,
                title=clip(title, 30),
                content_text=track.version_a,
                structured_json=structured,
                primary_asset_id=primary_asset_id,
            ),
        )


def send_photos_first() -> BotResponse:
    return BotResponse(
        title="先傳商品照片給我",
        bullets=("請拍 1 到 3 張商品照片傳給我", "再按「💎 寫珠寶推銷文案」"),
    )
