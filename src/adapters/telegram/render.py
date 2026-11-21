"""BotResponse -> one Telegram HTML message. Pure.

Telegram counts lengths in UTF-16 code units [inference: an emoji counts 2], so every limit here
is measured that way on the escaped HTML source, which is never shorter than the text Telegram
counts. Overflow is cut from the raw text and escaped again, so an entity is never split.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from src.domain.models import BotResponse, CopyableBlock

MESSAGE_MAX = 4096
CAPTION_MAX = 1024
COPY_BUTTON_MAX = 256
ELLIPSIS = "…"


@dataclass(frozen=True)
class Rendered:
    text: str
    markup: InlineKeyboardMarkup | None


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def fits_copy_button(body: str) -> bool:
    return 0 < utf16_len(body) <= COPY_BUTTON_MAX


def render(response: BotResponse, *, limit: int = MESSAGE_MAX) -> Rendered:
    bodies = [block.body_text for block in response.copyable_blocks]
    bullets = list(response.bullets)
    text = _compose(response, bullets, bodies)
    while (overflow := utf16_len(text) - limit) > 0:
        if any(bodies):
            parts = bodies
        elif any(bullets):
            parts = bullets
        else:
            break  # title and labels alone cannot reach the limit
        _level(parts, overflow)
        text = _compose(response, bullets, bodies)
    return Rendered(text=text, markup=_markup(response))


def _compose(response: BotResponse, bullets: list[str], bodies: list[str]) -> str:
    lines = [f"<b>{html.escape(response.title)}</b>"]
    lines += [f"• {html.escape(bullet)}" for bullet in bullets if bullet]
    for block, body in zip(response.copyable_blocks, bodies, strict=True):
        lines += ["", f"<b>{html.escape(block.label)}</b>"]
        if body:
            escaped = html.escape(body)
            lines.append(escaped if fits_copy_button(block.body_text) else f"<pre>{escaped}</pre>")
        if block.warning_note:
            lines.append(f"⚠️ {html.escape(block.warning_note)}")
    return "\n".join(lines)


def _level(parts: list[str], overflow: int) -> None:
    """Cut the longest parts down to one common escaped length, the highest that removes
    `overflow` units, so two long blocks both keep their opening lines."""
    sizes = [utf16_len(html.escape(part)) for part in parts]
    lo, hi = 0, max(sizes)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sum(max(0, size - mid) for size in sizes) >= overflow:
            lo = mid
        else:
            hi = mid - 1
    for i, size in enumerate(sizes):
        if size > lo:
            parts[i] = _cut(parts[i], lo)


def _cut(raw: str, max_escaped: int) -> str:
    """The longest prefix, plus an ellipsis, whose escaped form fits; empty when none does."""
    lo, hi = 0, len(raw) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if utf16_len(html.escape(raw[:mid] + ELLIPSIS)) <= max_escaped:
            lo = mid
        else:
            hi = mid - 1
    return raw[:lo] + ELLIPSIS if lo > 0 else ""


def _copy_button(block: CopyableBlock) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=f"📋 複製 {block.label}", copy_text=CopyTextButton(text=block.body_text)
    )


def _markup(response: BotResponse) -> InlineKeyboardMarkup | None:
    rows = [
        [_copy_button(block)]
        for block in response.copyable_blocks
        if fits_copy_button(block.body_text)
    ]
    rows += [
        [InlineKeyboardButton(text=action.label, callback_data=action.callback_data)]
        for action in response.actions
    ]
    return InlineKeyboardMarkup(rows) if rows else None
