import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.adapters.storage.ledger_repository import SqlLedger
from src.adapters.storage.media_repository import SqlMediaStore
from src.adapters.storage.repository import SqlStorage
from src.adapters.storage.session_repository import SqlSessionStore
from src.domain.media import BatchStatus, Intent, encode_callback
from src.domain.models import BotResponse, IncomingEvent, ReplyTarget
from src.domain.ports import LLMRequest
from src.domain.router import HandlerReply, Router, TurnContext
from src.domain.session import TopicSessionState
from src.pipeline.batch_aggregator import BatchAggregator
from src.pipeline.finops_guardian import FinOpsGuardian
from src.workflows.audio_handler import VoiceTranscriber
from src.workflows.dispatch import IntentDispatcher
from src.workflows.ios_helper import IosHelper
from src.workflows.lecture_digest import LectureDigest
from src.workflows.memory_stash import MemoryStash
from src.workflows.sales_copy import SalesCopy
from tests import imagegen
from tests.batch_helpers import BLUR_THRESHOLD, DEBOUNCE, batch_id_of
from tests.fakes import FakeChannel, FakeLLM, FakeNotifier, FrozenClock, Owner

Reply = str | Exception | Callable[[LLMRequest], str | Exception]

LECTURE_JSON = json.dumps(
    {
        "title": "銀髮族防詐騙講座",
        "takeaways": ["陌生來電先掛掉", "不要給驗證碼", "有疑問打 165"],
        "purpose": "教大家認出詐騙手法",
        "reminder": "接到可疑電話先問家人",
    },
    ensure_ascii=False,
)
CLEAN_A = "這條翡翠手鍊色澤溫潤，戴起來很有氣質，送給媽媽最貼心。"
SALES_JSON = json.dumps(
    {"category": "jewelry", "title": "溫潤翡翠手鍊", "version_a": CLEAN_A, "version_b": None},
    ensure_ascii=False,
)
ROUTE_SALES = json.dumps({"route": "sales", "topic_shift": False})


def sales_json(version_a: str, version_b: str | None = None, title: str = "翡翠手鍊") -> str:
    return json.dumps(
        {"category": "jewelry", "title": title, "version_a": version_a, "version_b": version_b},
        ensure_ascii=False,
    )


class WorkflowRig:
    def __init__(
        self,
        storage: SqlStorage,
        media: SqlMediaStore,
        sessions: SqlSessionStore,
        ledger: SqlLedger,
        clock: FrozenClock,
        owner: Owner,
        db_path: Path,
    ) -> None:
        self.db_path = db_path
        self.storage = storage
        self.media = media
        self.sessions = sessions
        self.ledger = ledger
        self.clock = clock
        self.owner = owner
        self.channel = FakeChannel()
        self.replies: dict[str, Reply] = {
            "route": ROUTE_SALES,
            "lecture_digest": LECTURE_JSON,
            "sales_copy": SALES_JSON,
        }
        self.llm = FakeLLM(provider="gemini", model="gemini-test", respond=self._respond)
        self.aggregator = BatchAggregator(
            media, self.channel, clock, blur_threshold=BLUR_THRESHOLD, debounce_s=DEBOUNCE
        )
        deps: dict[str, Any] = {
            "llm": self.llm,
            "storage": storage,
            "sessions": sessions,
            "batches": self.aggregator,
        }
        self.lecture = LectureDigest(**deps)
        self.sales = SalesCopy(**deps)
        self.memory = MemoryStash(**deps)
        self.ios = IosHelper(**deps)
        self.router = Router(
            finops=FinOpsGuardian(ledger, FakeNotifier(), clock),
            llm=self.llm,
            sessions=sessions,
            batches=self.aggregator,
            handlers={
                "general": _general,
                "lecture": self.lecture.follow_up,
                "sales": self.sales.follow_up,
                "memory": self.memory.handle,
                "ios_help": self.ios.handle,
            },
            transcriber=VoiceTranscriber(self.llm),
            intent_handler=IntentDispatcher(
                lecture=self.lecture, sales=self.sales, memory=self.memory, ios=self.ios
            ),
            clock=clock,
        )
        self._n = 0

    def _respond(self, req: LLMRequest) -> str | Exception:
        reply = self.replies[req.workflow_name]
        return reply(req) if callable(reply) else reply

    def calls(self, workflow: str) -> list[LLMRequest]:
        return [req for req in self.llm.calls if req.workflow_name == workflow]

    def _event(self, **fields: Any) -> IncomingEvent:
        self._n += 1
        return IncomingEvent.model_validate(
            {
                "event_id": f"w{self._n}",
                "tenant_id": self.owner.tenant_id,
                "user_id": self.owner.user_id,
                "channel": "telegram",
                "reply_target": ReplyTarget(address="chat-1"),
                **fields,
            }
        )

    async def photo_batch(self, n_images: int = 2) -> str:
        """Send a burst through the router; returns the batch id once it awaits an intent."""
        sent_before = len(self.channel.sent)
        for i in range(n_images):
            asset = await self.media.save_asset(
                tenant_id=self.owner.tenant_id,
                user_id=self.owner.user_id,
                data=imagegen.jpeg(imagegen.sharp(2000 + 37 * i + self._n)),
                media_type="image",
                ext="jpg",
            )
            assert (
                await self.router.handle(self._event(event_type="image", asset_ids=(asset,)))
                is None
            )
        await self.aggregator.drain()
        return batch_id_of(self.channel.sent[sent_before][1])

    async def tap(self, batch_id: str, intent: Intent) -> BotResponse | None:
        return await self.router.handle(
            self._event(
                event_type="action_callback", callback_data=encode_callback(batch_id, intent)
            )
        )

    async def say(self, text: str) -> BotResponse | None:
        return await self.router.handle(self._event(event_type="text", payload_text=text))

    async def spend_whole_budget(self) -> None:
        await self.ledger.record_usage(
            tenant_id=self.owner.tenant_id,
            user_id=self.owner.user_id,
            provider="deepseek",
            model="deepseek-flash",
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=Decimal("0.20"),
            workflow_name="test",
        )

    async def speak(self, duration_s: float = 8.0) -> BotResponse | None:
        """A voice message; replies["voice_transcribe"] decides what the clip says."""
        asset = await self.media.save_asset(
            tenant_id=self.owner.tenant_id,
            user_id=self.owner.user_id,
            data=b"OggS-voice",
            media_type="audio",
            ext="ogg",
        )
        return await self.router.handle(
            self._event(event_type="audio", asset_ids=(asset,), audio_duration_s=duration_s)
        )

    async def batch_status(self, batch_id: str) -> BatchStatus:
        batch = await self.media.get_batch(
            tenant_id=self.owner.tenant_id, user_id=self.owner.user_id, batch_id=batch_id
        )
        assert batch is not None
        return batch.status

    async def active(self) -> TopicSessionState | None:
        return await self.sessions.get_active(
            tenant_id=self.owner.tenant_id, user_id=self.owner.user_id
        )

    def knowledge_rows(self) -> list[tuple[str, str, str]]:
        """(id, category, content_text) of every knowledge item, oldest first."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT id, category, content_text FROM knowledge_items ORDER BY rowid"
            ).fetchall()


GENERAL = BotResponse(title="general")


async def _general(ctx: TurnContext) -> HandlerReply:
    return HandlerReply(text="general", response=GENERAL)
