"""Wires one running assistant from Settings. main.py adds PTB and the real providers; tests pass
a fake BotApi and fake providers and get the same object graph."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from src.adapters.discord_ops.notifier import DiscordWebhookNotifier
from src.adapters.llm.router_llm import RoutingLLM
from src.adapters.storage.engine import create_engine, db_path_for
from src.adapters.storage.ledger_repository import SqlLedger
from src.adapters.storage.media_repository import SqlMediaStore
from src.adapters.storage.repository import SqlStorage
from src.adapters.storage.seed import seed_allowed_users
from src.adapters.storage.session_repository import SqlSessionStore
from src.adapters.telegram.adapter import TelegramAdapter, TelegramChannel
from src.adapters.telegram.bot_api import BotApi
from src.config import Settings
from src.domain.ports import Clock, LLMPort, MediaStorePort, OpsNotifierPort, Provider
from src.domain.router import Router
from src.pipeline.batch_aggregator import BatchAggregator
from src.pipeline.finops_guardian import FinOpsGuardian
from src.workflows.audio_handler import VoiceTranscriber
from src.workflows.dispatch import IntentDispatcher
from src.workflows.general import general
from src.workflows.ios_helper import IosHelper
from src.workflows.lecture_digest import LectureDigest
from src.workflows.memory_stash import MemoryStash
from src.workflows.sales_copy import SalesCopy

logger = logging.getLogger(__name__)

# Gemini reads images and audio through the media store, so providers are built after it.
ProviderFactory = Callable[[MediaStorePort], Mapping[Provider, LLMPort]]


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    clock: Clock
    engine: AsyncEngine
    storage: SqlStorage
    media: SqlMediaStore
    aggregator: BatchAggregator
    adapter: TelegramAdapter
    http_client: httpx.AsyncClient


def build_runtime(
    settings: Settings,
    bot_api: BotApi,
    clock: Clock,
    providers: ProviderFactory,
    *,
    spares: ProviderFactory | None = None,
    notifier: OpsNotifierPort | None = None,
) -> Runtime:
    engine = create_engine(db_path_for(settings.DATA_DIR))
    http_client = httpx.AsyncClient()
    ops = notifier or DiscordWebhookNotifier(settings.DISCORD_OPS_WEBHOOK_URL, http_client)
    storage = SqlStorage(engine, clock)
    media = SqlMediaStore(engine, clock, settings.DATA_DIR)
    sessions = SqlSessionStore(engine, clock)
    finops = FinOpsGuardian(SqlLedger(engine, clock), ops, clock)
    llm = RoutingLLM(
        finops, providers(media), ops, clock, spares=spares(media) if spares is not None else None
    )
    channel = TelegramChannel(bot_api, storage, media)
    aggregator = BatchAggregator(
        media,
        channel,
        clock,
        blur_threshold=settings.BLUR_THRESHOLD,
        debounce_s=settings.BATCH_DEBOUNCE_SECONDS,
    )
    lecture = LectureDigest(llm=llm, storage=storage, sessions=sessions, batches=aggregator)
    sales = SalesCopy(llm=llm, storage=storage, sessions=sessions, batches=aggregator)
    memory = MemoryStash(llm=llm, storage=storage, sessions=sessions, batches=aggregator)
    ios = IosHelper(llm=llm, storage=storage, sessions=sessions, batches=aggregator)
    router = Router(
        finops=finops,
        llm=llm,
        sessions=sessions,
        batches=aggregator,
        handlers={
            "general": general,
            "lecture": lecture.follow_up,
            "sales": sales.follow_up,
            "memory": memory.handle,
            "ios_help": ios.handle,
        },
        transcriber=VoiceTranscriber(llm),
        intent_handler=IntentDispatcher(lecture=lecture, sales=sales, memory=memory, ios=ios),
        clock=clock,
    )
    adapter = TelegramAdapter(
        bot=bot_api,
        channel=channel,
        router=router,
        storage=storage,
        media=media,
        notifier=ops,
        allowed_user_ids=frozenset(settings.TELEGRAM_ALLOWED_USER_IDS),
        clock=clock,
    )
    return Runtime(settings, clock, engine, storage, media, aggregator, adapter, http_client)


async def start(runtime: Runtime) -> None:
    """Before the first update: every allowlisted account has a person, and photo bursts a
    restart interrupted get their one reply."""
    report = await seed_allowed_users(
        runtime.storage,
        runtime.clock,
        runtime.settings.TELEGRAM_ALLOWED_USER_IDS,
        runtime.settings.DEFAULT_DAILY_BUDGET_USD,
    )
    logger.info(
        "seeded telegram users: created=%s existing=%s", list(report.created), len(report.existing)
    )
    recovered = await runtime.aggregator.recover_orphans()
    logger.info("recovered %d interrupted photo batches", recovered)


async def drain(runtime: Runtime) -> None:
    """Send the replies of photo bursts still in their debounce window. The bot must still be
    able to send, so PTB calls this from post_stop."""
    await runtime.aggregator.drain()


async def stop(runtime: Runtime) -> None:
    await drain(runtime)
    await runtime.http_client.aclose()
    await runtime.engine.dispose()
