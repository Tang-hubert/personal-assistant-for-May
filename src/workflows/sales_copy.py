"""Dual-track sales copy (ARCHITECTURE §6). Version A always passes the deterministic scanner;
Version B, kept only when it carries a risky claim, ships with a legal warning. The Layer-2
follow-up uses the same dual track, because May usually adds her claims there."""

from __future__ import annotations

import logging
import re
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

logger = logging.getLogger(__name__)
# Keeps each sentence's own end mark, so a restored claim reads as she wrote it.
_SENTENCE_END = re.compile(r"(?<=[。！？!?\n])")

WORKFLOW = "sales_copy"
FOLLOWUP_WORKFLOW = "sales_copy_followup"
TEXT_WORKFLOW = "sales_copy_text"
MAX_SALES_IMAGES = 3
MAX_OUTPUT = 1500

_SCHEMA = (
    '只輸出 JSON：{"category":"jewelry 或 product","title":"15字內標題",'
    '"version_a":"安全版文案","version_b":"原意版文案或 null"}。'
    "version_a 用外觀、工藝、心情、送禮情境打動人，絕不寫療效、治病、保證效果或收入。"
    "長輩的訊息裡有功效、療效、保證或收益的說法時，version_b 一定要寫，"
    "而且一字不改保留她原本的那些說法（例如「保證有效」「降血壓」）；沒有就給 null。"
    "每版 300 字內。"
)
SALES_SYSTEM = TW_STYLE + "你幫一位長輩寫珠寶或商品的社群推銷文案。" + _SCHEMA
FOLLOWUP_SYSTEM = TW_STYLE + "[context] 是目前的安全版文案。依長輩這次的要求改寫。" + _SCHEMA
TEXT_SYSTEM = (
    TW_STYLE
    + "你幫一位長輩寫珠寶或商品的社群推銷文案。她沒有傳照片，"
    + "她的訊息就是原料（她寫的文章或口述），照她提供的內容寫，不要編造她沒說的細節。"
    + _SCHEMA
)


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


def risky_sentences(text: str) -> str:
    """Her own sentences that carry a prohibited claim, minus any leading instruction that
    ends in a colon (幫我改寫成文案 + colon)."""
    kept = []
    for sentence in _SENTENCE_END.split(text):
        claim = re.split(r"[：:]", sentence)[-1].strip()
        if claim and scan(claim):
            kept.append(claim)
    return "".join(claim if claim[-1] in "。！？!?" else f"{claim}。" for claim in kept)


def dual_track(out: SalesOut, her_words: str | None = None) -> DualTrack:
    """Version B keeps May's original intent. Gemini sometimes drops or softens it, so when
    her own words carry a risky claim that B lost, her risky sentences are put back."""
    hits_a = scan(out.version_a)
    version_a = clip(sanitize(out.version_a), BLOCK_MAX_CHARS)
    version_b = clip(out.version_b, BLOCK_MAX_CHARS) if out.version_b else None
    hits_b = scan(version_b) if version_b else ()
    if not hits_b and her_words and (claims := risky_sentences(her_words)):
        logger.info("version_b restored from her own claims")
        version_b = clip(f"{out.version_b or out.version_a}\n{claims}", BLOCK_MAX_CHARS)
        hits_b = scan(version_b)
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
            return await self._draft_from_text(ctx)
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
        track = dual_track(out, ctx.user_text)
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

    async def _draft_from_text(self, ctx: TurnContext) -> HandlerReply:
        """No artifact yet: May's own words are the material. Zero history (ADR-0003); the
        router turns the session into the Layer-2 follow-up through last_artifact_id."""
        tenant_id, user_id = ctx.event.tenant_id, ctx.event.user_id
        result = await self._llm.generate(
            LLMRequest(
                tenant_id=tenant_id,
                user_id=user_id,
                workflow_name=TEXT_WORKFLOW,
                capability=Cap.COPYWRITING_TW,
                prompt=PromptParts(system=TEXT_SYSTEM, current_user_msg=ctx.user_text),
                max_output_tokens=MAX_OUTPUT,
                json_output=True,
            )
        )
        out = parse_output(result, SalesOut, workflow=TEXT_WORKFLOW)
        track = dual_track(out, ctx.user_text)
        artifact_id = await self._save(tenant_id, user_id, out.category, out.title, track, None)
        return HandlerReply(
            text=f"已寫好文案：{track.version_a[:80]}",
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
