"""Routes a tapped intent button to its Layer-1 workflow (the Router's IntentHandler)."""

from __future__ import annotations

from src.domain.media import Intent, IntentSelected
from src.domain.models import BotResponse, IncomingEvent
from src.workflows.lecture_digest import LectureDigest
from src.workflows.sales_copy import SalesCopy


class IntentDispatcher:
    def __init__(self, *, lecture: LectureDigest, sales: SalesCopy) -> None:
        self._lecture = lecture
        self._sales = sales

    async def __call__(self, event: IncomingEvent, selected: IntentSelected) -> BotResponse:
        match selected.intent:
            case Intent.LECTURE:
                return await self._lecture.draft(event, selected)
            case Intent.SALES:
                return await self._sales.draft(event, selected)
            case Intent.MEMORY:
                # memory_stash arrives in T5; the batch stays awaiting_intent meanwhile.
                return memory_coming_soon()


def memory_coming_soon() -> BotResponse:
    return BotResponse(
        title="這個功能快好了",
        bullets=("「記下東西放哪了」下一版就能用", "這批照片我先幫您留著"),
    )
