import json

import pytest

from src.domain import replies
from src.domain.media import BatchStatus, Intent
from src.domain.models import TaskCapability
from src.domain.session import ARTIFACT_KEY, WORKFLOW_KEY
from src.workflows.common import TW_STYLE
from src.workflows.ios_helper import SCREENSHOT_HINT
from tests.workflow_helpers import WorkflowRig

ROUTE_IOS = json.dumps({"route": "ios_help", "topic_shift": False})


def _guide(title: str, *steps: str) -> str:
    return json.dumps({"title": title, "steps": list(steps)}, ensure_ascii=False)


async def test_a_phone_question_gets_three_steps_saved_as_a_guide(wf: WorkflowRig) -> None:
    wf.replies["route"] = ROUTE_IOS
    wf.replies["ios_helper"] = _guide(
        "放大字體", "打開設定", "點螢幕顯示與亮度", "點文字大小往右拉", "多的一步"
    )

    response = await wf.say("字太小了怎麼放大")

    assert response is not None
    assert response.bullets == (
        "第1步：打開設定",
        "第2步：點螢幕顯示與亮度",
        "第3步：點文字大小往右拉",
    )
    (call,) = wf.calls("ios_helper")
    assert call.capability is TaskCapability.COPYWRITING_TW
    assert call.prompt.recent_turns == () and call.prompt.retrieved_context == ""
    assert call.prompt.state_json == {}
    assert call.prompt.system.startswith(TW_STYLE)
    ((item_id, category, content),) = wf.knowledge_rows()
    assert category == "ios_guide" and "第3步" in content
    session = await wf.active()
    assert session is not None and session.state_json[ARTIFACT_KEY] == item_id


async def test_a_followup_sees_the_previous_guide_as_context(wf: WorkflowRig) -> None:
    wf.replies["route"] = ROUTE_IOS
    wf.replies["ios_helper"] = _guide("放大字體", "打開設定", "點螢幕顯示與亮度", "往右拉")
    await wf.say("字太小了怎麼放大")
    wf.replies["ios_helper"] = _guide("找不到時", "往下滑", "找到輔助使用", "點放大")

    await wf.say("我找不到螢幕顯示")

    _, second = wf.calls("ios_helper")
    assert "點螢幕顯示與亮度" in second.prompt.retrieved_context
    assert [turn.role for turn in second.prompt.recent_turns] == ["user", "assistant"]
    assert len(wf.knowledge_rows()) == 2


async def test_the_phone_button_reads_two_screenshots_and_opens_a_followup(
    wf: WorkflowRig,
) -> None:
    wf.replies["ios_screenshot"] = _guide("關掉通知", "點設定", "點通知", "關掉這個 App")
    batch_id = await wf.photo_batch(4)
    sent_before = len(wf.channel.sent)

    response = await wf.tap(batch_id, Intent.IOS)

    assert response is not None and len(response.bullets) == 3
    assert response.bullets[-1].endswith(SCREENSHOT_HINT)
    (call,) = wf.calls("ios_screenshot")
    assert call.capability is TaskCapability.MULTIMODAL_IMAGE
    assert len(call.asset_ids) == 2
    assert await wf.batch_status(batch_id) is BatchStatus.DONE
    ((item_id, category, _),) = wf.knowledge_rows()
    assert category == "ios_guide"
    session = await wf.active()
    assert session is not None and session.topic_type == "ios_help"
    assert session.state_json == {ARTIFACT_KEY: item_id, WORKFLOW_KEY: "ios_screenshot"}
    assert len(wf.channel.sent) == sent_before


@pytest.mark.parametrize("steps", [("打開設定",), ("打開設定", "點螢幕顯示與亮度")])
async def test_a_guide_with_fewer_than_three_steps_is_a_busy_reply(
    wf: WorkflowRig, steps: tuple[str, ...]
) -> None:
    wf.replies["route"] = ROUTE_IOS
    wf.replies["ios_helper"] = _guide("放大字體", *steps)

    assert await wf.say("字太小了") == replies.busy_message()
    assert wf.knowledge_rows() == []


async def test_a_two_step_screenshot_guide_leaves_the_batch_awaiting_intent(
    wf: WorkflowRig,
) -> None:
    wf.replies["ios_screenshot"] = _guide("關掉通知", "點設定", "點通知")
    batch_id = await wf.photo_batch(2)

    assert await wf.tap(batch_id, Intent.IOS) == replies.busy_message()
    assert await wf.batch_status(batch_id) is BatchStatus.AWAITING_INTENT
    assert wf.knowledge_rows() == []


async def test_a_phone_question_after_a_greeting_keeps_its_followups(wf: WorkflowRig) -> None:
    wf.replies["route"] = json.dumps({"route": "general", "topic_shift": False})
    await wf.say("你好")
    wf.replies["route"] = ROUTE_IOS
    wf.replies["ios_helper"] = _guide("截圖", "同時按側邊鍵", "和音量鍵", "放開")
    await wf.say("怎麼截圖")

    await wf.say("我找不到那個按鈕")

    _, second = wf.calls("ios_helper")
    assert "同時按側邊鍵" in second.prompt.retrieved_context
