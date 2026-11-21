from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from src.domain.models import BotResponse, ChatAction, ReplyTarget
from src.domain.ports import LLMRequest, LLMResult, Provider


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


@dataclass
class FakeNotifier:
    sent: list[tuple[str, str]] = field(default_factory=list)

    async def notify(self, level: str, message: str) -> None:
        self.sent.append((level, message))

    def levels(self) -> list[str]:
        return [level for level, _ in self.sent]


def _default_reply(request: LLMRequest) -> str | Exception:
    return "ok"


@dataclass
class FakeLLM:
    provider: Provider = "deepseek"
    model: str = "deepseek-flash"
    respond: Callable[[LLMRequest], str | Exception] = _default_reply
    calls: list[LLMRequest] = field(default_factory=list)

    async def generate(self, request: LLMRequest) -> LLMResult:
        self.calls.append(request)
        reply = self.respond(request)
        if isinstance(reply, Exception):
            raise reply
        return LLMResult(
            text=reply,
            provider=self.provider,
            model=self.model,
            prompt_tokens=1000,
            completion_tokens=100,
        )
