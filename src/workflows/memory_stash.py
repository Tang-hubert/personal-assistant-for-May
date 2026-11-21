"""Memory stash: "東西放哪了". A photo (📦) or a sentence saves where something is; a later
sentence moves it in place or asks where it is. Every place is an item_location knowledge row,
searched only within that category (ADR-0004), never held in chat history."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.domain import replies
from src.domain.budget import SLOT_RETRIEVED, PromptParts, truncate_to_tokens
from src.domain.media import IntentSelected
from src.domain.models import BotResponse, IncomingEvent, KnowledgeItem, NewKnowledgeItem
from src.domain.models import TaskCapability as Cap
from src.domain.ports import LLMPort, LLMRequest, SessionStorePort, StoragePort
from src.domain.router import HandlerReply, TurnContext
from src.domain.timeutil import taipei_date
from src.workflows.common import (
    BULLET_MAX_CHARS,
    TW_STYLE,
    BatchCompleter,
    clip,
    open_followup,
    parse_output,
    session_artifact,
)

WORKFLOW = "memory_stash"
PARSE_WORKFLOW = "memory_parse"
ANSWER_WORKFLOW = "memory_answer"
MAX_MEMORY_IMAGES = 3
MAX_RECORDS = 3
RECORD_MAX_TOKENS = 400
DRAFT_MAX_OUTPUT = 300
PARSE_MAX_OUTPUT = 200
ANSWER_MAX_OUTPUT = 300

PHOTO_SYSTEM = TW_STYLE + (
    "長輩拍了東西和它放的地方，請幫她記下來。只輸出 JSON："
    '{"item":"東西的名稱","location":"放的位置，照片看不出來就給空字串",'
    '"keywords":["最多 5 個之後找東西會用到的詞"]}。'
)
PARSE_SYSTEM = TW_STYLE + (
    "長輩在跟你說東西放在哪，或是在問東西放哪。[context] 若有，是剛剛記下的那一筆。只輸出 JSON："
    '{"action":"save|update|query","item":"東西名稱","location":"位置或空字串",'
    '"keywords":["最多 5 個搜尋用的詞"]}。'
    "save=記一樣新的東西；update=補充或更改 [context] 那一筆的位置；query=問東西放哪。"
)
ANSWER_SYSTEM = TW_STYLE + (
    "[context] 是長輩之前記下的東西和位置，每筆前面有編號。"
    "根據資料告訴她要找的東西放在哪，資料裡沒有的就老實說不知道，不要亂猜。只輸出 JSON："
    '{"bullets":["最多 3 句白話回答"],"pick":最符合的那一筆編號}。'
    "沒有任何一筆回答到她要找的東西時，pick 給 null。"
)


class StashOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    item: str = Field(min_length=1)
    location: str = ""
    keywords: tuple[str, ...] = ()


class MemoryAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["save", "update", "query"]
    item: str = ""
    location: str = ""
    keywords: tuple[str, ...] = ()


class AnswerOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    bullets: tuple[str, ...] = Field(min_length=1)
    pick: int | None = None


def _location(item: KnowledgeItem) -> str:
    location = item.structured_json.get("location")
    return location if isinstance(location, str) else ""


def _same_item(named: str, current: KnowledgeItem) -> bool:
    """No name means the current item. A different name is a different thing to remember,
    never a rename of the current one."""
    item = current.structured_json.get("item")
    known = (item if isinstance(item, str) else current.title).strip()
    named = named.strip()
    return not named or named in known or known in named


def _content(item: str, location: str) -> str:
    return f"{item}｜{location}" if location else item


def _record(n: int, item: KnowledgeItem) -> str:
    line = f"[{n}] {taipei_date(item.created_at).isoformat()} {item.title}｜{_location(item)}"
    return truncate_to_tokens(line, RECORD_MAX_TOKENS)


def stashed(item: str, location: str) -> BotResponse:
    if location:
        bullets = (clip(f"放在：{location}", BULLET_MAX_CHARS), "之後換地方 直接跟我說就好")
    else:
        bullets = ("還不知道放在哪裡", "直接打字或講語音告訴我放哪")
    return BotResponse(
        title=clip(f"記好了：{item}", 30), bullets=bullets, actions=(replies.finish_button(),)
    )


def not_found() -> BotResponse:
    return BotResponse(
        title="我還沒記過這個",
        bullets=("我找不到這樣東西的紀錄", "放好的時候拍照或跟我說 我幫您記下來"),
    )


def tell_me_what() -> BotResponse:
    return BotResponse(
        title="要記什麼東西呢",
        bullets=("請告訴我是什麼東西 放在哪裡", "例如：翡翠手鍊放在紅櫃子第二層"),
    )


class MemoryStash:
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
        assets = selected.kept_asset_ids[:MAX_MEMORY_IMAGES]
        result = await self._llm.generate(
            LLMRequest(
                tenant_id=tenant_id,
                user_id=user_id,
                workflow_name=WORKFLOW,
                capability=Cap.MULTIMODAL_IMAGE,
                prompt=PromptParts(system=PHOTO_SYSTEM, current_user_msg="請幫我記下這個放哪"),
                asset_ids=assets,
                max_output_tokens=DRAFT_MAX_OUTPUT,
                json_output=True,
            )
        )
        out = parse_output(result, StashOut)
        artifact_id = await self._save(
            tenant_id, user_id, out.item, out.location, out.keywords, assets[0] if assets else None
        )
        await open_followup(
            self._sessions,
            tenant_id=tenant_id,
            user_id=user_id,
            topic_type="memory",
            workflow=WORKFLOW,
            artifact_id=artifact_id,
        )
        await self._batches.complete(
            tenant_id=tenant_id, user_id=user_id, batch_id=selected.batch_id
        )
        return stashed(out.item, out.location)

    async def handle(self, ctx: TurnContext) -> HandlerReply:
        tenant_id, user_id = ctx.event.tenant_id, ctx.event.user_id
        current = await session_artifact(
            self._storage, tenant_id=tenant_id, user_id=user_id, session=ctx.session
        )
        if current is not None and current.category != "item_location":
            current = None
        context = f"{current.title}｜{_location(current)}" if current is not None else ""
        result = await self._llm.generate(
            LLMRequest(
                tenant_id=tenant_id,
                user_id=user_id,
                workflow_name=PARSE_WORKFLOW,
                capability=Cap.TEXT_FAST_ROUTE,
                prompt=PromptParts(
                    system=PARSE_SYSTEM,
                    retrieved_context=truncate_to_tokens(context, RECORD_MAX_TOKENS),
                    current_user_msg=ctx.user_text,
                ),
                max_output_tokens=PARSE_MAX_OUTPUT,
                json_output=True,
            )
        )
        action = parse_output(result, MemoryAction)
        if action.action == "query":
            return await self._query(ctx, action)
        if action.action == "update" and current is not None and _same_item(action.item, current):
            item = action.item or current.title
            location = action.location or _location(current)
            await self._storage.update_knowledge(
                tenant_id=tenant_id,
                user_id=user_id,
                item_id=current.id,
                title=clip(item, 30),
                content_text=_content(item, location),
                structured_json={"item": item, "location": location},
                keywords=action.keywords,
            )
            return HandlerReply(
                text=f"已更新：{_content(item, location)}", response=stashed(item, location)
            )
        if not action.item:
            return HandlerReply(text="請告訴我要記什麼", response=tell_me_what())
        artifact_id = await self._save(
            tenant_id, user_id, action.item, action.location, action.keywords, None
        )
        return HandlerReply(
            text=f"記好了：{_content(action.item, action.location)}",
            response=stashed(action.item, action.location),
            last_artifact_id=artifact_id,
        )

    async def _query(self, ctx: TurnContext, action: MemoryAction) -> HandlerReply:
        tenant_id, user_id = ctx.event.tenant_id, ctx.event.user_id
        query = " ".join((action.item, *action.keywords)).strip() or ctx.user_text
        hits = await self._storage.search_knowledge(
            tenant_id=tenant_id,
            user_id=user_id,
            query=query,
            limit=MAX_RECORDS,
            categories=["item_location"],
        )
        if not hits:
            return HandlerReply(text="還沒記過這個", response=not_found())
        context = "\n".join(_record(n, hit) for n, hit in enumerate(hits, 1))
        result = await self._llm.generate(
            LLMRequest(
                tenant_id=tenant_id,
                user_id=user_id,
                workflow_name=ANSWER_WORKFLOW,
                capability=Cap.TEXT_FAST_ROUTE,
                prompt=PromptParts(
                    system=ANSWER_SYSTEM,
                    retrieved_context=truncate_to_tokens(context, SLOT_RETRIEVED),
                    current_user_msg=ctx.user_text,
                ),
                max_output_tokens=ANSWER_MAX_OUTPUT,
                json_output=True,
            )
        )
        out = parse_output(result, AnswerOut)
        bullets = tuple(clip(b, BULLET_MAX_CHARS) for b in out.bullets[:3])
        text = "\n".join(bullets)
        if out.pick is None or not 1 <= out.pick <= len(hits):
            return HandlerReply(
                text=text,
                response=BotResponse(
                    title="我查了一下", bullets=bullets, actions=(replies.finish_button(),)
                ),
            )
        picked = hits[out.pick - 1]
        return HandlerReply(
            text=text,
            response=BotResponse(
                title=clip(f"找到了：{picked.title}", 30),
                bullets=bullets,
                image_asset_id=picked.primary_asset_id,
                actions=(replies.finish_button(),),
            ),
            # "我把它移到保險箱了" right after this answer moves the record it found.
            last_artifact_id=picked.id,
        )

    async def _save(
        self,
        tenant_id: str,
        user_id: str,
        item: str,
        location: str,
        keywords: tuple[str, ...],
        primary_asset_id: str | None,
    ) -> str:
        return await self._storage.save_knowledge(
            tenant_id=tenant_id,
            user_id=user_id,
            item=NewKnowledgeItem(
                category="item_location",
                title=clip(item, 30),
                content_text=_content(item, location),
                structured_json={"item": item, "location": location},
                primary_asset_id=primary_asset_id,
            ),
            keywords=keywords,
        )
