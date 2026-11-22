import json

from src.domain import replies
from src.domain.media import BatchStatus, Intent, intent_already_done
from src.domain.models import TaskCapability
from src.domain.ports import BudgetExhausted, LLMUnavailable
from src.domain.session import ARTIFACT_KEY
from src.workflows.lecture_digest import LECTURE_SYSTEM
from tests.workflow_helpers import LECTURE_JSON, WorkflowRig


async def test_lecture_tap_returns_one_digest_with_three_takeaways(wf: WorkflowRig) -> None:
    batch_id = await wf.photo_batch(3)
    response = await wf.tap(batch_id, Intent.LECTURE)

    assert response is not None
    assert response.title == "銀髮族防詐騙講座"
    assert response.bullets == ("陌生來電先掛掉", "不要給驗證碼", "有疑問打 165")
    assert response.copyable_blocks[0].label == "這場講座的目的／溫馨提醒"
    assert "接到可疑電話先問家人" in response.copyable_blocks[0].body_text
    assert replies.finish_button() in response.actions
    assert len(wf.channel.sent) == 1  # only the intent prompt; the digest is the return value


async def test_lecture_call_is_multimodal_with_zero_history(wf: WorkflowRig) -> None:
    await wf.say("我想寫一段翡翠手鍊的文案")  # leaves an active session holding turns
    before = await wf.active()
    assert before is not None and before.recent_turns

    await wf.tap(await wf.photo_batch(2), Intent.LECTURE)

    (call,) = wf.calls("lecture_digest")
    assert call.capability is TaskCapability.MULTIMODAL_IMAGE
    assert len(call.asset_ids) == 2
    assert call.prompt.recent_turns == ()
    assert call.prompt.state_json == {}
    assert call.prompt.retrieved_context == ""


async def test_completion_archives_and_opens_one_clean_followup_session(wf: WorkflowRig) -> None:
    await wf.tap(await wf.photo_batch(), Intent.LECTURE)

    ((item_id, category, content),) = wf.knowledge_rows()
    assert category == "lecture"
    assert "陌生來電先掛掉" in content
    session = await wf.active()
    assert session is not None
    assert session.topic_type == "lecture"
    assert session.state_json == {ARTIFACT_KEY: item_id, "workflow": "lecture_digest"}
    assert session.recent_turns == ()


async def test_a_second_tap_on_the_same_intent_costs_nothing(wf: WorkflowRig) -> None:
    batch_id = await wf.photo_batch()
    await wf.tap(batch_id, Intent.LECTURE)
    assert await wf.finished_intents(batch_id) == (Intent.LECTURE,)

    again = await wf.tap(batch_id, Intent.LECTURE)
    assert again == intent_already_done()
    assert len(wf.calls("lecture_digest")) == 1


async def test_the_same_photos_can_then_run_a_different_intent(wf: WorkflowRig) -> None:
    """T5.1 live finding: after 寫珠寶 the batch refused 教我用手機 as already done."""
    batch_id = await wf.photo_batch()
    await wf.tap(batch_id, Intent.LECTURE)
    wf.replies["ios_screenshot"] = json.dumps({"title": "看投影片", "steps": ["一", "二", "三"]})

    other = await wf.tap(batch_id, Intent.IOS)

    assert other is not None and other.title == "看投影片"
    assert await wf.finished_intents(batch_id) == (Intent.LECTURE, Intent.IOS)
    assert await wf.batch_status(batch_id) is BatchStatus.AWAITING_INTENT


async def test_provider_failure_keeps_the_batch_tappable(wf: WorkflowRig) -> None:
    batch_id = await wf.photo_batch()
    wf.replies["lecture_digest"] = LLMUnavailable("provider_error", "gemini")

    assert await wf.tap(batch_id, Intent.LECTURE) == replies.busy_message()
    assert await wf.batch_status(batch_id) is BatchStatus.AWAITING_INTENT
    assert wf.knowledge_rows() == []
    assert await wf.active() is None

    wf.replies["lecture_digest"] = LECTURE_JSON
    retry = await wf.tap(batch_id, Intent.LECTURE)
    assert retry is not None and retry.title == "銀髮族防詐騙講座"
    assert await wf.finished_intents(batch_id) == (Intent.LECTURE,)


async def test_malformed_reply_is_treated_as_a_provider_failure(wf: WorkflowRig) -> None:
    batch_id = await wf.photo_batch()
    wf.replies["lecture_digest"] = '{"title": "沒有重點"'

    assert await wf.tap(batch_id, Intent.LECTURE) == replies.busy_message()
    assert await wf.batch_status(batch_id) is BatchStatus.AWAITING_INTENT
    assert wf.knowledge_rows() == []


async def test_budget_exhausted_mid_workflow_answers_rest_and_keeps_the_batch(
    wf: WorkflowRig,
) -> None:
    batch_id = await wf.photo_batch()
    wf.replies["lecture_digest"] = BudgetExhausted()

    assert await wf.tap(batch_id, Intent.LECTURE) == replies.rest_message()
    assert await wf.batch_status(batch_id) is BatchStatus.AWAITING_INTENT


def test_lecture_prompt_asks_for_taiwan_mandarin() -> None:
    assert "台灣日常口語" in LECTURE_SYSTEM
    assert "避免中國大陸用語" in LECTURE_SYSTEM
