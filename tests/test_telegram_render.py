import re
from html.parser import HTMLParser

import pytest

from src.adapters.telegram.render import CAPTION_MAX, MESSAGE_MAX, render, utf16_len
from src.domain import replies
from src.domain.models import ActionButton, BotResponse, CopyableBlock


def _with_block(body: str, **block: object) -> BotResponse:
    return BotResponse(
        title="文案",
        bullets=("A 版可以放心直接貼",),
        copyable_blocks=(CopyableBlock(label="A 版", body_text=body, **block),),  # type: ignore[arg-type]
        actions=(replies.finish_button(),),
    )


def _copy_texts(response: BotResponse) -> list[str]:
    markup = render(response).markup
    assert markup is not None
    return [
        button.copy_text.text
        for row in markup.inline_keyboard
        for button in row
        if button.copy_text is not None
    ]


class _Balanced(HTMLParser):
    """Fails on a split entity or an unclosed tag, the way Telegram rejects the message."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack.pop() == tag


def _assert_valid_html(text: str) -> None:
    assert not re.search(r"&(?!(?:amp|lt|gt|quot|#x27);)", text), "split or raw entity"
    parser = _Balanced()
    parser.feed(text)
    parser.close()
    assert parser.stack == []


@pytest.mark.parametrize("length", [255, 256])
def test_a_block_up_to_256_units_gets_a_copy_button(length: int) -> None:
    body = "翠" * length
    assert _copy_texts(_with_block(body)) == [body]
    assert "<pre>" not in render(_with_block(body)).text
    assert body in render(_with_block(body)).text


def test_a_257_unit_block_becomes_a_pre_block_without_a_copy_button() -> None:
    body = "翠" * 257
    rendered = render(_with_block(body))
    assert f"<pre>{body}</pre>" in rendered.text
    assert _copy_texts(_with_block(body)) == []


def test_emoji_count_twice_so_a_200_char_block_with_emoji_uses_pre() -> None:
    body = "💎" * 130  # 130 chars, 260 UTF-16 units
    assert len(body) < 256 < utf16_len(body)
    assert _copy_texts(_with_block(body)) == []
    assert "<pre>" in render(_with_block(body)).text


def test_a_1000_char_pre_block_and_html_in_text_are_escaped() -> None:
    body = "<script>&" + "翠" * 991
    rendered = render(
        BotResponse(
            title="<b>標題</b>",
            bullets=("a < b & c",),
            copyable_blocks=(
                CopyableBlock(label="A&B", body_text=body, warning_note="注意 <違法> & 罰款"),
            ),
        )
    )
    assert "<script>" not in rendered.text
    assert "&lt;script&gt;&amp;" in rendered.text
    assert "<b>&lt;b&gt;標題&lt;/b&gt;</b>" in rendered.text
    assert "• a &lt; b &amp; c" in rendered.text
    assert "⚠️ 注意 &lt;違法&gt; &amp; 罰款" in rendered.text
    _assert_valid_html(rendered.text)


def test_two_1000_char_blocks_of_ampersands_fit_one_valid_message() -> None:
    response = BotResponse(
        title="文案",
        bullets=("一", "二", "三"),
        copyable_blocks=(
            CopyableBlock(label="A", body_text="&" * 1000),
            CopyableBlock(label="B", body_text="&" * 1000, warning_note="⚠" * 300),
        ),
    )

    text = render(response).text

    assert utf16_len(text) <= MESSAGE_MAX
    _assert_valid_html(text)
    assert text.count("<pre>") == 2
    assert "• 一" in text


def test_a_photo_caption_fits_1024_units() -> None:
    response = BotResponse(
        title="找到了",
        bullets=("💎" * 400, "翡翠" * 300, "第三句"),
        image_asset_id="a1",
    )

    text = render(response, limit=CAPTION_MAX).text

    assert utf16_len(text) <= CAPTION_MAX
    _assert_valid_html(text)
    assert text.startswith("<b>找到了</b>")


def test_actions_are_one_button_per_row_after_the_copy_buttons() -> None:
    response = BotResponse(
        title="t",
        copyable_blocks=(CopyableBlock(label="A", body_text="短"),),
        actions=(
            ActionButton(label="一", callback_data="x:1"),
            ActionButton(label="二", callback_data="x:2"),
        ),
    )
    markup = render(response).markup
    assert markup is not None
    rows = markup.inline_keyboard
    assert [len(row) for row in rows] == [1, 1, 1]
    assert rows[0][0].copy_text is not None
    assert [row[0].callback_data for row in rows[1:]] == ["x:1", "x:2"]


def test_a_plain_reply_has_no_keyboard() -> None:
    assert render(replies.busy_message()).markup is None
