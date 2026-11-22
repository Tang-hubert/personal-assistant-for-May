"""T5.1 #4: pasted text or a voice note, with no photos, still gets dual-track copy or a lecture
digest, saved without a photo and followed up in Layer 2."""

import json
import sqlite3
from contextlib import closing

from src.domain.compliance import scan
from src.domain.session import ARTIFACT_KEY
from src.workflows.lecture_digest import send_material_first
from tests.workflow_helpers import CLEAN_A, LECTURE_JSON, ROUTE_SALES, WorkflowRig, sales_json

ROUTE_LECTURE = json.dumps({"route": "lecture", "topic_shift": False})
ARTICLE = "這條翡翠手鍊是緬甸A貨，綠色很均勻，我戴了三年，睡得比較好，想賣給老朋友。"
NOTES = (
    "今天里民中心的講座在講防詐騙。講師說陌生來電要先掛掉，不要給驗證碼，"
    "有疑問就打 165 反詐騙專線，也可以先問家人再決定。"
)


def _primary_asset(wf: WorkflowRig, item_id: str) -> object:
    with closing(sqlite3.connect(wf.db_path)) as conn:
        return conn.execute(
            "SELECT primary_asset_id FROM knowledge_items WHERE id = ?", (item_id,)
        ).fetchone()[0]


async def test_pasted_text_becomes_dual_track_copy_without_photos(wf: WorkflowRig) -> None:
    wf.replies["route"] = ROUTE_SALES
    wf.replies["sales_copy_text"] = sales_json(
        "戴起來能改善失眠，色澤溫潤好看。", "戴了會改善失眠喔！"
    )

    response = await wf.say(ARTICLE)

    assert response is not None
    block_a, block_b = response.copyable_blocks
    assert scan(block_a.body_text) == ()
    assert block_b.warning_note is not None
    (call,) = wf.calls("sales_copy_text")
    assert ARTICLE in call.prompt.current_user_msg
    assert call.prompt.recent_turns == () and call.prompt.retrieved_context == ""
    ((item_id, category, _),) = wf.knowledge_rows()
    assert category == "jewelry"
    assert _primary_asset(wf, item_id) is None
    session = await wf.active()
    assert session is not None and session.state_json[ARTIFACT_KEY] == item_id


async def test_a_tone_tweak_after_a_text_draft_is_a_followup(wf: WorkflowRig) -> None:
    wf.replies["route"] = ROUTE_SALES
    wf.replies["sales_copy_text"] = sales_json(CLEAN_A)
    await wf.say(ARTICLE)
    wf.replies["sales_copy_followup"] = sales_json("更親切的版本，送媽媽最貼心。")

    await wf.say("改親切一點")

    (call,) = wf.calls("sales_copy_followup")
    assert CLEAN_A in call.prompt.retrieved_context
    assert len(wf.calls("sales_copy_text")) == 1


async def test_lecture_notes_as_text_become_a_saved_digest(wf: WorkflowRig) -> None:
    wf.replies["route"] = ROUTE_LECTURE
    wf.replies["lecture_digest_text"] = LECTURE_JSON

    response = await wf.say(NOTES)

    assert response is not None and response.title == "銀髮族防詐騙講座"
    assert len(response.bullets) == 3
    assert "溫馨提醒" in response.copyable_blocks[0].body_text
    ((item_id, category, _),) = wf.knowledge_rows()
    assert category == "lecture"
    assert _primary_asset(wf, item_id) is None
    session = await wf.active()
    assert session is not None and session.state_json[ARTIFACT_KEY] == item_id
    assert session.topic_type == "lecture"


async def test_a_voice_note_takes_the_same_text_path(wf: WorkflowRig) -> None:
    wf.replies["voice_transcribe"] = json.dumps({"transcript": NOTES}, ensure_ascii=False)
    wf.replies["route"] = ROUTE_LECTURE
    wf.replies["lecture_digest_text"] = LECTURE_JSON

    response = await wf.speak()

    assert response is not None and response.title == "銀髮族防詐騙講座"
    assert NOTES in wf.calls("lecture_digest_text")[0].prompt.current_user_msg


async def test_a_short_lecture_question_without_notes_asks_for_material(wf: WorkflowRig) -> None:
    wf.replies["route"] = ROUTE_LECTURE

    assert await wf.say("我想問今天講座的事") == send_material_first()
    assert wf.calls("lecture_digest_text") == []
    assert wf.knowledge_rows() == []
