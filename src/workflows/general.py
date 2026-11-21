"""The fallback route: greetings, /start, and anything unclassified get the fixed menu (Q4)."""

from __future__ import annotations

from src.domain import replies
from src.domain.router import HandlerReply, TurnContext


async def general(ctx: TurnContext) -> HandlerReply:
    return HandlerReply(text="（功能選單）", response=replies.help_menu())
