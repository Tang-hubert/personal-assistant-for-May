"""Routes a tapped intent button to its Layer-1 workflow (the Router's IntentHandler)."""

from __future__ import annotations

from src.domain.media import Intent, IntentSelected
from src.domain.models import BotResponse, IncomingEvent
from src.workflows.ios_helper import IosHelper
from src.workflows.lecture_digest import LectureDigest
from src.workflows.memory_stash import MemoryStash
from src.workflows.sales_copy import SalesCopy


class IntentDispatcher:
    def __init__(
        self, *, lecture: LectureDigest, sales: SalesCopy, memory: MemoryStash, ios: IosHelper
    ) -> None:
        self._lecture = lecture
        self._sales = sales
        self._memory = memory
        self._ios = ios

    async def __call__(self, event: IncomingEvent, selected: IntentSelected) -> BotResponse:
        match selected.intent:
            case Intent.LECTURE:
                return await self._lecture.draft(event, selected)
            case Intent.SALES:
                return await self._sales.draft(event, selected)
            case Intent.MEMORY:
                return await self._memory.draft(event, selected)
            case Intent.IOS:
                return await self._ios.draft(event, selected)
