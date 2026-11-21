"""What the Layer-1 workflows share: the Taiwan tone rule, output parsing, and the hand-off
to a clean Layer-2 follow-up session (ARCHITECTURE §4.3)."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ValidationError

from src.domain.models import KnowledgeItem
from src.domain.ports import LLMResult, LLMUnavailable, SessionStorePort, StoragePort
from src.domain.session import ARTIFACT_KEY, WORKFLOW_KEY, TopicSessionState, TopicType

TW_STYLE = (
    "請用繁體中文（台灣日常口語、親切好讀）回答，避免中國大陸用語"
    "（例如：視頻→影片、軟件→軟體、親→您、質量→品質、信息→資訊）。"
)
BULLET_MAX_CHARS = 60
BLOCK_MAX_CHARS = 1000


class BatchCompleter(Protocol):
    async def complete(self, *, tenant_id: str, user_id: str, batch_id: str) -> bool: ...


def parse_output[T: BaseModel](result: LLMResult, model: type[T]) -> T:
    """An unusable reply counts as a provider failure: the router answers busy and the batch
    stays awaiting_intent so May can tap again. RoutingLLM has already billed the call."""
    try:
        return model.model_validate_json(result.text)
    except ValidationError as err:
        raise LLMUnavailable("provider_error", result.provider) from err


def clip(text: str, max_chars: int) -> str:
    text = text.strip()
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


async def open_followup(
    sessions: SessionStorePort,
    *,
    tenant_id: str,
    user_id: str,
    topic_type: TopicType,
    workflow: str,
    artifact_id: str,
) -> None:
    """A reference to the artifact, never its text, and no turns."""
    await sessions.open_fresh(
        tenant_id=tenant_id,
        user_id=user_id,
        topic_type=topic_type,
        state_json={ARTIFACT_KEY: artifact_id, WORKFLOW_KEY: workflow},
    )


async def session_artifact(
    storage: StoragePort, *, tenant_id: str, user_id: str, session: TopicSessionState
) -> KnowledgeItem | None:
    artifact_id = session.state_json.get(ARTIFACT_KEY)
    if not isinstance(artifact_id, str):
        return None
    return await storage.get_knowledge(tenant_id=tenant_id, user_id=user_id, item_id=artifact_id)
