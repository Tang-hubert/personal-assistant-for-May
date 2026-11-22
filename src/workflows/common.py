"""What the Layer-1 workflows share: the Taiwan tone rule, output parsing, and the hand-off
to a clean Layer-2 follow-up session (ARCHITECTURE §4.3)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Annotated, Protocol

from pydantic import BaseModel, BeforeValidator, ValidationError

from src.domain.media import Intent
from src.domain.models import KnowledgeItem
from src.domain.ports import LLMResult, LLMUnavailable, SessionStorePort, StoragePort
from src.domain.session import ARTIFACT_KEY, WORKFLOW_KEY, TopicSessionState, TopicType

TW_STYLE = (
    "請用繁體中文（台灣日常口語、親切好讀）回答，避免中國大陸用語"
    "（例如：視頻→影片、軟件→軟體、親→您、質量→品質、信息→資訊）。"
)
BULLET_MAX_CHARS = 60
BLOCK_MAX_CHARS = 1000
TITLE_MAX_CHARS = 30

logger = logging.getLogger(__name__)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", re.DOTALL)


class BatchCompleter(Protocol):
    async def complete(
        self, *, tenant_id: str, user_id: str, batch_id: str, intent: Intent
    ) -> bool: ...


def parse_output[T: BaseModel](result: LLMResult, model: type[T], *, workflow: str) -> T:
    """An unusable reply counts as a provider failure: the router answers busy and the batch
    stays awaiting_intent so May can tap again. RoutingLLM has already billed the call.
    The warning names the failing fields and error types, never the reply text."""
    text = result.text.strip()
    if fenced := _FENCE.match(text):
        text = fenced.group(1)
    try:
        return model.model_validate_json(text)
    except ValidationError as err:
        problems = ",".join(
            f"{'.'.join(str(part) for part in e['loc']) or '<root>'}:{e['type']}"
            for e in err.errors(include_input=False, include_url=False)
        )
        logger.warning(
            "parse failed workflow=%s schema=%s provider=%s model=%s finish=%s errors=%s",
            workflow,
            model.__name__,
            result.provider,
            result.model,
            result.finish_reason,
            problems,
        )
        raise LLMUnavailable(
            "provider_error", result.provider, detail=f"parse {model.__name__}"
        ) from err


def clip(text: str, max_chars: int) -> str:
    text = text.strip()
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _clip_title(value: object) -> object:
    return clip(value, TITLE_MAX_CHARS) if isinstance(value, str) else value


ClippedTitle = Annotated[str, BeforeValidator(_clip_title)]

_ITEM_KEYS = ("text", "step", "content")


def _item_text(item: object) -> str | None:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in _ITEM_KEYS:
            if isinstance(item.get(key), str):
                return str(item[key])
        return next((v for v in item.values() if isinstance(v, str)), None)
    return None


def text_list(limit: int, max_chars: int) -> Callable[[object], object]:
    """Model drift is normalized, not rejected: one string, or a list of strings or small
    dicts, becomes at most `limit` non-empty items of at most `max_chars` each."""

    def normalize(value: object) -> object:
        items = [value] if isinstance(value, str) else value
        if not isinstance(items, list | tuple):
            return value  # the field's own type reports it
        texts = (_item_text(item) for item in items)
        return tuple(clip(t, max_chars) for t in texts if t and t.strip())[:limit]

    return normalize


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
