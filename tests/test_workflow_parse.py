import logging

import pytest

from src.domain.ports import LLMResult, LLMUnavailable
from src.workflows.common import parse_output
from src.workflows.ios_helper import GuideOut
from src.workflows.lecture_digest import FollowupOut, LectureOut
from src.workflows.sales_copy import SalesOut


def _result(text: str, finish: str | None = "STOP") -> LLMResult:
    return LLMResult(
        text=text,
        provider="gemini",
        model="gemini-3.8-flash",
        prompt_tokens=10,
        completion_tokens=10,
        finish_reason=finish,
    )


def test_a_markdown_json_fence_is_tolerated() -> None:
    text = '```json\n{"title":"調亮螢幕","steps":["一","二","三"]}\n```'
    out = parse_output(_result(text), GuideOut, workflow="ios_helper")
    assert out.steps == ("一", "二", "三")


def test_a_long_title_is_clipped_not_rejected() -> None:
    title = "清" * 45
    out = parse_output(
        _result(f'{{"title":"{title}","steps":["一","二","三"]}}'), GuideOut, workflow="ios"
    )
    assert len(out.title) == 30
    assert out.title.endswith("…")


def test_extra_steps_are_cut_to_three_and_each_step_is_clipped() -> None:
    long_step = "按" * 80
    text = f'{{"title":"t","steps":["{long_step}","二","三","四","五"]}}'
    out = parse_output(_result(text), GuideOut, workflow="ios")
    assert len(out.steps) == 3
    assert len(out.steps[0]) <= 60
    assert out.steps[1:] == ("二", "三")


def test_steps_as_dicts_or_one_string_are_normalized() -> None:
    text = (
        '{"title":"t","steps":'
        '[{"step":1,"text":"打開設定"},{"content":"點一般"},{"x":"點儲存空間"}]}'
    )
    out = parse_output(_result(text), GuideOut, workflow="ios")
    assert out.steps == ("打開設定", "點一般", "點儲存空間")

    single = parse_output(_result('{"title":"t","steps":"打開設定"}'), GuideOut, workflow="ios")
    assert single.steps == ("打開設定",)


def test_fewer_than_three_steps_still_answer() -> None:
    out = parse_output(_result('{"title":"t","steps":["一","",  "二"]}'), GuideOut, workflow="ios")
    assert out.steps == ("一", "二")


def test_lecture_takeaways_and_followup_bullets_are_capped_at_three() -> None:
    lecture = parse_output(
        _result('{"title":"t","takeaways":["a","b","c","d"],"purpose":"p","reminder":"r"}'),
        LectureOut,
        workflow="lecture_digest",
    )
    assert lecture.takeaways == ("a", "b", "c")
    followup = parse_output(
        _result('{"bullets":["a","b","c","d","e"]}'), FollowupOut, workflow="lecture_followup"
    )
    assert followup.bullets == ("a", "b", "c")


def test_sales_title_is_clipped() -> None:
    out = parse_output(
        _result(f'{{"title":"{"美" * 40}","version_a":"好看"}}'), SalesOut, workflow="sales"
    )
    assert len(out.title) == 30


def test_an_unusable_reply_logs_a_warning_without_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING), pytest.raises(LLMUnavailable) as caught:
        parse_output(
            _result('{"title":"秘密內容', finish="MAX_TOKENS"), GuideOut, workflow="ios_helper"
        )

    assert caught.value.reason == "provider_error"
    assert caught.value.detail == "parse GuideOut"
    assert "workflow=ios_helper" in caplog.text
    assert "schema=GuideOut" in caplog.text
    assert "finish=MAX_TOKENS" in caplog.text
    assert "json_invalid" in caplog.text
    assert "秘密" not in caplog.text


def test_a_missing_field_names_the_field_in_the_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING), pytest.raises(LLMUnavailable):
        parse_output(_result('{"title":"t"}'), GuideOut, workflow="ios_helper")
    assert "steps:missing" in caplog.text
