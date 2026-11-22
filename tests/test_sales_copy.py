import json

from src.domain import replies
from src.domain.compliance import scan
from src.domain.media import BatchStatus, Intent
from src.domain.models import TaskCapability
from src.domain.ports import LLMUnavailable
from src.domain.session import ARTIFACT_KEY
from src.workflows.sales_copy import SALES_SYSTEM
from tests.workflow_helpers import CLEAN_A, WorkflowRig, sales_json

DIRTY_A = "每天戴著可以治療關節痛，保證有效，送給媽媽最貼心。"


async def test_sales_tap_returns_one_clean_version_a(wf: WorkflowRig) -> None:
    response = await wf.tap(await wf.photo_batch(5), Intent.SALES)

    assert response is not None
    (block,) = response.copyable_blocks
    assert block.body_text == CLEAN_A
    assert block.warning_note is None
    (call,) = wf.calls("sales_copy")
    assert call.capability is TaskCapability.MULTIMODAL_IMAGE
    assert len(call.asset_ids) == 3


async def test_leaked_claims_never_reach_version_a(wf: WorkflowRig) -> None:
    wf.replies["sales_copy"] = sales_json(DIRTY_A)
    response = await wf.tap(await wf.photo_batch(), Intent.SALES)

    assert response is not None
    version_a = response.copyable_blocks[0].body_text
    assert scan(version_a) == ()
    assert "送給媽媽最貼心" in version_a
    ((_, category, content),) = wf.knowledge_rows()
    assert category == "jewelry"
    assert scan(content) == ()


async def test_risky_version_b_is_kept_with_a_legal_warning(wf: WorkflowRig) -> None:
    wf.replies["sales_copy"] = sales_json(CLEAN_A, "戴了可以降血壓、改善失眠")
    response = await wf.tap(await wf.photo_batch(), Intent.SALES)

    assert response is not None
    block_a, block_b = response.copyable_blocks
    assert scan(block_a.body_text) == ()
    assert block_b.body_text == "戴了可以降血壓、改善失眠"
    assert block_b.warning_note is not None
    assert "降血壓" in block_b.warning_note and "藥事法第69條" in block_b.warning_note


async def test_version_b_without_risk_or_empty_is_dropped(wf: WorkflowRig) -> None:
    for version_b in ("顏色很漂亮，很適合送禮", None, ""):
        wf.replies["sales_copy"] = sales_json(CLEAN_A, version_b)
        response = await wf.tap(await wf.photo_batch(), Intent.SALES)
        assert response is not None
        assert len(response.copyable_blocks) == 1


async def test_overlong_copy_is_clipped_to_the_block_limit(wf: WorkflowRig) -> None:
    wf.replies["sales_copy"] = sales_json("溫潤" * 700, "可以治療" * 400)
    response = await wf.tap(await wf.photo_batch(), Intent.SALES)

    assert response is not None
    assert all(len(b.body_text) <= 1000 for b in response.copyable_blocks)
    assert len(response.copyable_blocks) == 2


async def test_sales_starts_with_zero_history_and_one_followup_session(wf: WorkflowRig) -> None:
    wf.replies["route"] = json.dumps({"route": "general", "topic_shift": False})
    await wf.say("先聊點別的")
    await wf.tap(await wf.photo_batch(), Intent.SALES)

    (call,) = wf.calls("sales_copy")
    assert call.prompt.recent_turns == () and call.prompt.state_json == {}
    ((item_id, _, _),) = wf.knowledge_rows()
    session = await wf.active()
    assert session is not None
    assert session.topic_type == "sales"
    assert session.state_json == {ARTIFACT_KEY: item_id, "workflow": "sales_copy"}
    assert session.recent_turns == ()


async def test_sales_failure_keeps_the_batch_awaiting(wf: WorkflowRig) -> None:
    batch_id = await wf.photo_batch()
    for failure in (LLMUnavailable("rate_limited", "gemini"), "not json"):
        wf.replies["sales_copy"] = failure
        assert await wf.tap(batch_id, Intent.SALES) == replies.busy_message()
        assert await wf.batch_status(batch_id) is BatchStatus.AWAITING_INTENT
    assert wf.knowledge_rows() == []


def test_sales_prompt_carries_taiwan_tone_and_dual_track_rules() -> None:
    assert "台灣日常口語" in SALES_SYSTEM and "避免中國大陸用語" in SALES_SYSTEM
    assert "version_a" in SALES_SYSTEM and "version_b" in SALES_SYSTEM
    assert "絕不寫療效" in SALES_SYSTEM
