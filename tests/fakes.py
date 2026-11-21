from dataclasses import dataclass, field
from datetime import datetime

from src.domain.models import BotResponse, ChatAction, ReplyTarget


@dataclass
class FrozenClock:
    instant: datetime

    def now(self) -> datetime:
        return self.instant


@dataclass
class FakeChannel:
    sent: list[tuple[ReplyTarget, BotResponse]] = field(default_factory=list)
    chat_actions: list[tuple[ReplyTarget, ChatAction]] = field(default_factory=list)

    async def send(self, target: ReplyTarget, response: BotResponse) -> None:
        self.sent.append((target, response))

    async def send_chat_action(self, target: ReplyTarget, action: ChatAction) -> None:
        self.chat_actions.append((target, action))


@dataclass(frozen=True)
class Owner:
    tenant_id: str
    user_id: str
