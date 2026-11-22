import json

from src.domain import replies
from src.domain.budget import FREE_TEXT_MAX_CHARS
from src.domain.media import Intent
from src.domain.models import TaskCapability
from src.domain.session import ARTIFACT_KEY
from tests.workflow_helpers import WorkflowRig, sales_json


def _heard(text: str) -> str:
    return json.dumps({"transcript": text, "audible": True}, ensure_ascii=False)


async def test_a_121_second_voice_is_refused_with_zero_llm_calls(wf: WorkflowRig) -> None:
    assert await wf.speak(121.0) == replies.voice_too_long()
    assert wf.llm.calls == []


async def test_a_half_second_voice_is_unclear_with_zero_llm_calls(wf: WorkflowRig) -> None:
    assert await wf.speak(0.5) == replies.voice_unclear()
    assert wf.llm.calls == []


async def test_silence_is_unclear_after_one_transcription_and_no_routing(
    wf: WorkflowRig,
) -> None:
    wf.replies["voice_transcribe"] = json.dumps({"transcript": "嗯", "audible": False})

    assert await wf.speak() == replies.voice_unclear()
    assert [req.workflow_name for req in wf.llm.calls] == ["voice_transcribe"]
    assert await wf.active() is None


async def test_the_transcription_call_carries_the_clip_and_its_duration(wf: WorkflowRig) -> None:
    wf.replies["voice_transcribe"] = _heard("你好")
    wf.replies["route"] = json.dumps({"route": "general", "topic_shift": False})
    await wf.speak(12.0)

    (call,) = wf.calls("voice_transcribe")
    assert call.capability is TaskCapability.MULTIMODAL_AUDIO
    assert call.audio_duration_s == 12.0
    assert len(call.asset_ids) == 1
    assert call.prompt.recent_turns == ()


async def test_a_long_transcript_is_cut_to_850_chars_and_the_reply_says_so(
    wf: WorkflowRig,
) -> None:
    wf.replies["voice_transcribe"] = _heard("翡" * 900)
    wf.replies["route"] = json.dumps({"route": "general", "topic_shift": False})

    response = await wf.speak()

    assert response is not None
    assert response.bullets[-1].endswith(replies.TRUNCATED_NOTICE)
    (route,) = wf.calls("route")
    assert route.prompt.current_user_msg == "翡" * FREE_TEXT_MAX_CHARS


async def test_an_exhausted_budget_answers_rest_before_transcribing(wf: WorkflowRig) -> None:
    await wf.spend_whole_budget()

    assert await wf.speak() == replies.rest_message()
    assert wf.llm.calls == []


async def test_voice_inside_a_sales_followup_rewrites_the_copy(wf: WorkflowRig) -> None:
    await wf.tap(await wf.photo_batch(), Intent.SALES)
    first = await wf.active()
    assert first is not None
    sent_before = len(wf.channel.sent)
    spoken = "售價一千二，可以刻字"
    wf.replies["voice_transcribe"] = _heard(spoken)
    wf.replies["sales_copy_followup"] = sales_json("溫潤翡翠手鍊，售價一千二，可以免費刻字。")
    workflows_before = len(wf.llm.calls)

    response = await wf.speak()

    assert response is not None
    assert "一千二" in response.copyable_blocks[0].body_text
    assert [req.workflow_name for req in wf.llm.calls[workflows_before:]] == [
        "voice_transcribe",
        "route",
        "sales_copy_followup",
    ]
    assert wf.calls("sales_copy_followup")[0].prompt.current_user_msg == spoken
    after = await wf.active()
    assert after is not None and after.id == first.id
    assert after.state_json[ARTIFACT_KEY] != first.state_json[ARTIFACT_KEY]
    assert after.recent_turns[0].text == spoken
    assert len(wf.channel.sent) == sent_before


async def test_a_broken_transcription_reply_is_the_busy_message(wf: WorkflowRig) -> None:
    wf.replies["voice_transcribe"] = "not json"

    assert await wf.speak() == replies.busy_message()
    assert wf.calls("route") == []
