import json
from datetime import timedelta

from src.domain.budget import MAX_TURNS, SLOT_TURNS, turns_tokens
from src.domain.compliance import scan
from src.domain.media import Intent
from src.domain.session import ARTIFACT_KEY
from src.workflows.sales_copy import send_photos_first
from tests.workflow_helpers import CLEAN_A, WorkflowRig, sales_json

WARMER_A = "這條翡翠手鍊溫潤又有氣質，媽媽戴起來一定很開心喔！"


async def _sales_done(wf: WorkflowRig) -> str:
    await wf.tap(await wf.photo_batch(), Intent.SALES)
    session = await wf.active()
    assert session is not None
    return str(session.state_json[ARTIFACT_KEY])


async def test_tone_tweak_uses_the_artifact_as_context_not_state(wf: WorkflowRig) -> None:
    await _sales_done(wf)
    wf.clock.instant += timedelta(minutes=5)
    wf.replies["sales_copy_followup"] = sales_json(WARMER_A)

    response = await wf.say("改親切一點")

    assert response is not None
    assert response.copyable_blocks[0].body_text == WARMER_A
    (call,) = wf.calls("sales_copy_followup")
    assert CLEAN_A in call.prompt.retrieved_context
    assert CLEAN_A not in json.dumps(call.prompt.state_json, ensure_ascii=False)


async def test_followup_with_medical_claims_returns_dual_track(wf: WorkflowRig) -> None:
    await _sales_done(wf)
    wf.replies["sales_copy_followup"] = sales_json(
        "戴著能改善失眠、降血壓，溫潤又好看。", "這條手鍊可以改善失眠、降血壓喔！"
    )

    response = await wf.say("幫我加上改善失眠、降血壓")

    assert response is not None
    block_a, block_b = response.copyable_blocks
    assert scan(block_a.body_text) == ()
    assert "溫潤又好看" in block_a.body_text
    assert {h.term for h in scan(block_b.body_text)} >= {"改善失眠", "降血壓"}
    assert block_b.warning_note is not None and "降血壓" in block_b.warning_note


async def test_each_rewrite_becomes_the_base_of_the_next(wf: WorkflowRig) -> None:
    first_id = await _sales_done(wf)
    wf.replies["sales_copy_followup"] = sales_json(WARMER_A)
    await wf.say("改親切一點")

    session = await wf.active()
    assert session is not None
    second_id = session.state_json[ARTIFACT_KEY]
    assert second_id != first_id
    assert [row[0] for row in wf.knowledge_rows()] == [first_id, second_id]

    await wf.say("再短一點")
    second_call = wf.calls("sales_copy_followup")[1]
    assert WARMER_A in second_call.prompt.retrieved_context


async def test_followup_after_idle_window_starts_clean(wf: WorkflowRig) -> None:
    await _sales_done(wf)
    wf.clock.instant += timedelta(minutes=16)

    response = await wf.say("改親切一點")

    assert response == send_photos_first()
    assert wf.calls("sales_copy_followup") == []
    session = await wf.active()
    assert session is not None and ARTIFACT_KEY not in session.state_json


async def test_followup_sticks_to_the_artifact_workflow_unless_the_topic_shifts(
    wf: WorkflowRig,
) -> None:
    await _sales_done(wf)
    wf.replies["route"] = json.dumps({"route": "general", "topic_shift": False})
    wf.replies["sales_copy_followup"] = sales_json(WARMER_A)
    await wf.say("改親切一點")
    assert len(wf.calls("sales_copy_followup")) == 1

    wf.replies["route"] = json.dumps({"route": "lecture", "topic_shift": True})
    await wf.say("我想問今天講座的事")
    session = await wf.active()
    assert session is not None
    assert session.topic_type == "lecture"
    assert ARTIFACT_KEY not in session.state_json


async def test_lecture_followup_answers_from_the_lecture_notes(wf: WorkflowRig) -> None:
    await wf.tap(await wf.photo_batch(), Intent.LECTURE)
    wf.replies["route"] = json.dumps({"route": "lecture", "topic_shift": False})
    wf.replies["lecture_followup"] = json.dumps(
        {"bullets": ["打 165 反詐騙專線"]}, ensure_ascii=False
    )

    response = await wf.say("有問題要打幾號?")

    assert response is not None and response.bullets == ("打 165 反詐騙專線",)
    (call,) = wf.calls("lecture_followup")
    assert "有疑問打 165" in call.prompt.retrieved_context


async def test_repeated_followups_keep_the_layer2_bounds(wf: WorkflowRig) -> None:
    await _sales_done(wf)
    wf.replies["sales_copy_followup"] = sales_json("溫潤" * 450)
    wf.replies["slot_extract"] = json.dumps({"topic": "翡翠文案", "facts": ["要親切"]})
    for i in range(8):
        response = await wf.say(f"第 {i} 次改寫，請再親切一點" * 10)
        session = await wf.active()
        if session is None:
            break
        assert len(session.recent_turns) <= MAX_TURNS
        assert turns_tokens(session.recent_turns) <= SLOT_TURNS
        assert response is not None
