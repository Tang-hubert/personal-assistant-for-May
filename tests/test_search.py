import sqlite3

import pytest
from sqlalchemy import delete, text

from src.adapters.storage.orm import KnowledgeItemRow
from src.adapters.storage.repository import SqlStorage
from src.domain.models import NewKnowledgeItem
from src.domain.search_tokens import build_search_tokens


def test_plain_unicode61_over_raw_text_misses_two_char_word() -> None:
    """Why search_tokens exists (ADR-0004): unicode61 keeps a CJK run as one token."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE raw USING fts5(content, tokenize='unicode61')")
    conn.execute("INSERT INTO raw(content) VALUES ('翡翠放紅櫃子')")
    hits = conn.execute("SELECT content FROM raw WHERE raw MATCH '\"翡翠\"'").fetchall()
    conn.close()
    assert hits == []


def test_search_tokens_hold_cjk_bigrams_words_and_keywords() -> None:
    tokens = build_search_tokens("翡翠放紅櫃子 Jade 1", keywords=["翡翠", "紅色櫃子"]).split()
    assert {"翡翠", "翠放", "放紅", "紅櫃", "櫃子", "jade", "1", "紅色", "色櫃"} <= set(tokens)
    assert len(tokens) == len(set(tokens))


def test_single_cjk_char_is_kept_as_a_token() -> None:
    assert build_search_tokens("玉 在 盒", keywords=()).split() == ["玉", "在", "盒"]


async def _person(storage: SqlStorage) -> tuple[str, str]:
    tenant_id = await storage.create_tenant(name="May")
    user_id = await storage.create_user(tenant_id=tenant_id, display_name="May")
    return tenant_id, user_id


def _item(content: str) -> NewKnowledgeItem:
    return NewKnowledgeItem(category="item_location", title="", content_text=content)


async def test_two_char_chinese_query_finds_item(storage: SqlStorage) -> None:
    tenant_id, user_id = await _person(storage)
    item_id = await storage.save_knowledge(
        tenant_id=tenant_id, user_id=user_id, item=_item("翡翠放紅櫃子")
    )
    await storage.save_knowledge(tenant_id=tenant_id, user_id=user_id, item=_item("鑰匙在玄關"))

    hits = await storage.search_knowledge(tenant_id=tenant_id, user_id=user_id, query="翡翠")
    assert [hit.id for hit in hits] == [item_id]


async def test_query_inside_a_sentence_finds_item(storage: SqlStorage) -> None:
    tenant_id, user_id = await _person(storage)
    item_id = await storage.save_knowledge(
        tenant_id=tenant_id, user_id=user_id, item=_item("翡翠放紅櫃子")
    )
    hits = await storage.search_knowledge(
        tenant_id=tenant_id, user_id=user_id, query="我的翡翠放哪裡了？"
    )
    assert hits[0].id == item_id


async def test_search_returns_at_most_three(storage: SqlStorage) -> None:
    tenant_id, user_id = await _person(storage)
    for place in ("紅櫃子", "藍盒子", "抽屜", "床頭", "包包"):
        await storage.save_knowledge(
            tenant_id=tenant_id, user_id=user_id, item=_item(f"翡翠放{place}")
        )
    hits = await storage.search_knowledge(tenant_id=tenant_id, user_id=user_id, query="翡翠")
    assert len(hits) == 3


async def test_fts_index_follows_updates_and_deletes(storage: SqlStorage) -> None:
    tenant_id, user_id = await _person(storage)
    item_id = await storage.save_knowledge(
        tenant_id=tenant_id, user_id=user_id, item=_item("翡翠放紅櫃子")
    )
    async with storage.session() as session:
        row = await session.get(KnowledgeItemRow, item_id)
        assert row is not None
        row.search_tokens = build_search_tokens("珍珠在藍盒子", keywords=())
        await session.commit()
    assert await storage.search_knowledge(tenant_id=tenant_id, user_id=user_id, query="翡翠") == []
    assert await storage.search_knowledge(tenant_id=tenant_id, user_id=user_id, query="珍珠")

    async with storage.session() as session:
        await session.execute(delete(KnowledgeItemRow).where(KnowledgeItemRow.id == item_id))
        await session.commit()
        fts_rows = (
            await session.execute(text("SELECT count(*) FROM knowledge_items_fts"))
        ).scalar()
    assert fts_rows == 0


@pytest.mark.parametrize("query", ['"', "*", "OR", "翡翠 OR", 'a" OR "b', "NEAR(", "", "？？"])
async def test_fts_syntax_in_query_never_breaks(storage: SqlStorage, query: str) -> None:
    tenant_id, user_id = await _person(storage)
    await storage.save_knowledge(tenant_id=tenant_id, user_id=user_id, item=_item("翡翠放紅櫃子"))
    hits = await storage.search_knowledge(tenant_id=tenant_id, user_id=user_id, query=query)
    assert isinstance(hits, list)
