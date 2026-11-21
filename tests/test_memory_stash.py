import json
from datetime import timedelta

import pytest

from src.domain import replies
from src.domain.budget import SLOT_RETRIEVED, estimate_tokens
from src.domain.media import BatchStatus, Intent
from src.domain.models import NewKnowledgeItem, TaskCapability
from src.domain.ports import LLMUnavailable
from src.domain.session import ARTIFACT_KEY, WORKFLOW_KEY
from src.workflows.common import TW_STYLE
from src.workflows.memory_stash import not_found
from tests.workflow_helpers import WorkflowRig

ROUTE_MEMORY = json.dumps({"route": "memory", "topic_shift": False})


def _stash(item: str, location: str = "", keywords: tuple[str, ...] = ()) -> str:
    return json.dumps(
        {"item": item, "location": location, "keywords": list(keywords)}, ensure_ascii=False
    )


def _action(action: str, item: str = "", location: str = "", *keywords: str) -> str:
    return json.dumps(
        {"action": action, "item": item, "location": location, "keywords": list(keywords)},
        ensure_ascii=False,
    )


def _answer(pick: int, *bullets: str) -> str:
    return json.dumps({"bullets": list(bullets), "pick": pick}, ensure_ascii=False)


def _memory(wf: WorkflowRig) -> WorkflowRig:
    wf.replies["route"] = ROUTE_MEMORY
    return wf


async def _place(wf: WorkflowRig, item: str, location: str, asset: str | None = None) -> str:
    return await wf.storage.save_knowledge(
        tenant_id=wf.owner.tenant_id,
        user_id=wf.owner.user_id,
        item=NewKnowledgeItem(
            category="item_location",
            title=item,
            content_text=f"{item}｜{location}",
            structured_json={"item": item, "location": location},
            primary_asset_id=asset,
        ),
        keywords=[item],
    )


async def test_the_box_button_saves_an_item_location_and_opens_a_clean_followup(
    wf: WorkflowRig,
) -> None:
    wf.replies["memory_stash"] = _stash("翡翠手鍊", "紅櫃子", ("翡翠", "手鍊"))
    batch_id = await wf.photo_batch(5)
    sent_before = len(wf.channel.sent)

    response = await wf.tap(batch_id, Intent.MEMORY)

    assert response is not None and response.title == "記好了：翡翠手鍊"
    (call,) = wf.calls("memory_stash")
    assert call.capability is TaskCapability.MULTIMODAL_IMAGE
    assert len(call.asset_ids) == 3
    assert call.prompt.system.startswith(TW_STYLE)
    ((item_id, category, content),) = wf.knowledge_rows()
    assert (category, content) == ("item_location", "翡翠手鍊｜紅櫃子")
    item = await wf.storage.get_knowledge(
        tenant_id=wf.owner.tenant_id, user_id=wf.owner.user_id, item_id=item_id
    )
    assert item is not None and item.primary_asset_id == call.asset_ids[0]
    hits = await wf.storage.search_knowledge(
        tenant_id=wf.owner.tenant_id, user_id=wf.owner.user_id, query="手鍊"
    )
    assert [hit.id for hit in hits] == [item_id]
    session = await wf.active()
    assert session is not None and session.topic_type == "memory"
    assert session.state_json == {ARTIFACT_KEY: item_id, WORKFLOW_KEY: "memory_stash"}
    assert session.recent_turns == ()
    assert await wf.batch_status(batch_id) is BatchStatus.DONE
    assert len(wf.channel.sent) == sent_before


async def test_a_photo_without_a_visible_place_asks_where_it_is(wf: WorkflowRig) -> None:
    wf.replies["memory_stash"] = _stash("護照")

    response = await wf.tap(await wf.photo_batch(), Intent.MEMORY)

    assert response is not None
    assert any("告訴我放哪" in bullet for bullet in response.bullets)
    assert replies.finish_button() in response.actions


async def test_saying_the_place_afterwards_updates_the_same_item(wf: WorkflowRig) -> None:
    wf.replies["memory_stash"] = _stash("護照")
    await wf.tap(await wf.photo_batch(), Intent.MEMORY)
    ((item_id, _, _),) = wf.knowledge_rows()
    wf.replies["route"] = json.dumps({"route": "general", "topic_shift": False})
    wf.replies["memory_parse"] = _action("update", "", "紅櫃子第二層", "紅櫃子")

    response = await wf.say("紅櫃子第二層")

    assert response is not None and "紅櫃子第二層" in response.bullets[0]
    (parse,) = wf.calls("memory_parse")
    assert "護照" in parse.prompt.retrieved_context
    assert parse.prompt.recent_turns == ()
    assert wf.knowledge_rows() == [(item_id, "item_location", "護照｜紅櫃子第二層")]
    hits = await wf.storage.search_knowledge(
        tenant_id=wf.owner.tenant_id, user_id=wf.owner.user_id, query="紅櫃子"
    )
    assert [hit.id for hit in hits] == [item_id]


async def test_a_spoken_place_also_updates_the_item(wf: WorkflowRig) -> None:
    wf.replies["memory_stash"] = _stash("護照")
    await wf.tap(await wf.photo_batch(), Intent.MEMORY)
    wf.replies["voice_transcribe"] = json.dumps(
        {"transcript": "放在書桌抽屜", "audible": True}, ensure_ascii=False
    )
    wf.replies["memory_parse"] = _action("update", "", "書桌抽屜")

    await wf.speak()

    ((_, _, content),) = wf.knowledge_rows()
    assert content == "護照｜書桌抽屜"


async def test_a_typed_memory_is_saved_without_an_asset(wf: WorkflowRig) -> None:
    _memory(wf).replies["memory_parse"] = _action("save", "印章", "電視櫃左邊", "印章")

    response = await wf.say("印章放在電視櫃左邊")

    assert response is not None and response.title == "記好了：印章"
    ((item_id, category, _),) = wf.knowledge_rows()
    assert category == "item_location"
    item = await wf.storage.get_knowledge(
        tenant_id=wf.owner.tenant_id, user_id=wf.owner.user_id, item_id=item_id
    )
    assert item is not None and item.primary_asset_id is None
    session = await wf.active()
    assert session is not None and session.state_json[ARTIFACT_KEY] == item_id


async def test_a_query_sends_at_most_three_records_and_shows_the_picked_photo(
    wf: WorkflowRig,
) -> None:
    long_place = "客廳" + "很長的位置說明" * 200
    for n in range(5):
        await _place(wf, "翡翠", f"{long_place}{n}", asset=f"asset-{n}")
    await wf.storage.save_knowledge(
        tenant_id=wf.owner.tenant_id,
        user_id=wf.owner.user_id,
        item=NewKnowledgeItem(category="jewelry", title="翡翠文案", content_text="翡翠溫潤"),
        keywords=["翡翠"],
    )
    _memory(wf).replies["memory_parse"] = _action("query", "翡翠", "", "翡翠")
    wf.replies["memory_answer"] = _answer(2, "翡翠在客廳")

    response = await wf.say("我的翡翠放哪了")

    (answer,) = wf.calls("memory_answer")
    context = answer.prompt.retrieved_context
    assert context.count("\n[") == 2 and context.startswith("[1]")
    assert "文案" not in context
    assert estimate_tokens(context) <= SLOT_RETRIEVED
    assert answer.capability is TaskCapability.TEXT_FAST_ROUTE
    assert answer.prompt.system.startswith(TW_STYLE)
    assert answer.prompt.recent_turns == ()
    assert wf.calls("memory_parse")[0].prompt.recent_turns == ()
    assert response is not None
    assert response.bullets == ("翡翠在客廳",)
    hits = await wf.storage.search_knowledge(
        tenant_id=wf.owner.tenant_id,
        user_id=wf.owner.user_id,
        query="翡翠",
        categories=["item_location"],
    )
    assert response.image_asset_id == hits[1].primary_asset_id


async def test_a_query_with_no_record_answers_without_a_second_llm_call(
    wf: WorkflowRig,
) -> None:
    await _place(wf, "印章", "電視櫃")
    _memory(wf).replies["memory_parse"] = _action("query", "護照", "", "護照")

    assert await wf.say("護照放哪") == not_found()
    assert wf.calls("memory_answer") == []


async def test_a_query_keeps_working_in_a_session_opened_by_a_photo(wf: WorkflowRig) -> None:
    wf.replies["memory_stash"] = _stash("護照", "書桌")
    await wf.tap(await wf.photo_batch(), Intent.MEMORY)
    wf.clock.instant += timedelta(minutes=2)
    wf.replies["memory_parse"] = _action("query", "護照", "", "護照")
    wf.replies["memory_answer"] = _answer(1, "護照在書桌")

    response = await wf.say("護照放哪")

    assert response is not None and response.bullets == ("護照在書桌",)
    assert response.image_asset_id is not None


async def test_a_failed_photo_reply_leaves_the_batch_awaiting_intent(wf: WorkflowRig) -> None:
    wf.replies["memory_stash"] = LLMUnavailable("provider_error", "gemini")
    batch_id = await wf.photo_batch()

    assert await wf.tap(batch_id, Intent.MEMORY) == replies.busy_message()
    assert await wf.batch_status(batch_id) is BatchStatus.AWAITING_INTENT
    assert wf.knowledge_rows() == []


@pytest.mark.parametrize("pick", [None, 4], ids=["null", "out-of-range"])
async def test_an_answer_that_picks_no_record_does_not_claim_it_was_found(
    wf: WorkflowRig, pick: int | None
) -> None:
    await _place(wf, "翡翠手鍊", "紅櫃子第二層", asset="asset-1")
    _memory(wf).replies["memory_parse"] = _action("query", "櫃子鑰匙", "", "鑰匙", "櫃子")
    wf.replies["memory_answer"] = json.dumps(
        {"bullets": ["我沒有記過櫃子鑰匙放哪裡"], "pick": pick}, ensure_ascii=False
    )

    response = await wf.say("櫃子鑰匙放哪了")

    assert response is not None and "找到了" not in response.title
    assert response.bullets == ("我沒有記過櫃子鑰匙放哪裡",)
    assert response.image_asset_id is None
    session = await wf.active()
    assert session is not None and ARTIFACT_KEY not in session.state_json


async def test_moving_an_item_right_after_finding_it_updates_it_in_place(
    wf: WorkflowRig,
) -> None:
    item_id = await _place(wf, "翡翠手鍊", "紅櫃子")
    _memory(wf).replies["memory_parse"] = _action("query", "翡翠手鍊", "", "翡翠")
    wf.replies["memory_answer"] = _answer(1, "在紅櫃子")
    await wf.say("翡翠手鍊放哪了")
    wf.replies["memory_parse"] = _action("update", "翡翠手鍊", "保險箱", "翡翠")

    await wf.say("我把它移到保險箱了")

    assert wf.knowledge_rows() == [(item_id, "item_location", "翡翠手鍊｜保險箱")]


async def test_an_update_naming_another_item_saves_it_and_keeps_the_current_one(
    wf: WorkflowRig,
) -> None:
    _memory(wf).replies["memory_parse"] = _action("save", "印章", "電視櫃", "印章")
    await wf.say("印章放在電視櫃")
    wf.replies["memory_parse"] = _action("update", "護照", "保險箱", "護照")

    response = await wf.say("護照我改放保險箱了")

    assert response is not None and response.title == "記好了：護照"
    rows = {content: item_id for item_id, _, content in wf.knowledge_rows()}
    assert set(rows) == {"印章｜電視櫃", "護照｜保險箱"}
    session = await wf.active()
    assert session is not None and session.state_json[ARTIFACT_KEY] == rows["護照｜保險箱"]


async def test_a_place_said_after_a_greeting_can_be_corrected(wf: WorkflowRig) -> None:
    wf.replies["route"] = json.dumps({"route": "general", "topic_shift": False})
    await wf.say("你好")
    _memory(wf).replies["memory_parse"] = _action("save", "印章", "電視櫃", "印章")
    await wf.say("印章放在電視櫃")
    wf.replies["memory_parse"] = _action("update", "印章", "書房", "印章")

    response = await wf.say("不對 改放書房")

    assert response is not None and "書房" in response.bullets[0]
    ((_, _, content),) = wf.knowledge_rows()
    assert content == "印章｜書房"
