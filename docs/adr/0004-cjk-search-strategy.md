# ADR-0004: Portable CJK Search via `search_tokens`

- **Status**: Accepted (Architect ruling v1.3, 2026-11-21)
- **Source findings**: Review 2.9, 2.10

## Context
SQLite FTS5's default `unicode61` tokenizer treats a contiguous run of CJK characters as one token. After saving "翡翠放紅櫃子", a search for "翡翠" would miss, which breaks Memory Stash ("東西放哪了"). The `trigram` tokenizer requires at least 3 characters per query, so 2-character words such as 翡翠 still miss. FTS5 virtual tables and triggers are also SQLite-only and invisible to Alembic autogenerate, which may try to drop them.

## Decision
1. `knowledge_items` gets a portable `search_tokens TEXT` column, written on every save. It contains:
   - space-separated keywords extracted by DeepSeek (`TEXT_FAST_ROUTE`), and
   - Python-generated character bigrams of the CJK text.
2. An SQLite FTS5 virtual table indexes `search_tokens`. It and its sync triggers are created in a **hand-written** Alembic migration. `env.py` uses `include_object` to exclude FTS internal tables from autogenerate.
   - The FTS table keeps its own copy of `search_tokens` keyed by an `item_id UNINDEXED` column, not external content. `knowledge_items` has a TEXT primary key, so its implicit `rowid` can be renumbered by `VACUUM`, which would silently desync an external-content index (T1).
3. Queries go through `StoragePort.search_knowledge()`. Query text is tokenized the same way (keywords + bigrams) and returns the top 3.
4. Phase 3 Postgres implements `search_knowledge()` over the same `search_tokens` column. The column is portable; the FTS5 index is not.

## Invariants
1. Every `knowledge_items` write sets `search_tokens` (repository-enforced, not caller convention).
2. Any 2-character CJK sequence present in the original text is findable.
3. Retrieval injects at most 3 records, ≤ 1,200 tokens total (ADR-0003 shared `retrieved_context` slot, v1.3.1).
4. Alembic autogenerate never emits drops for FTS objects.

## Consequences
- Save calls DeepSeek once for keywords. If keyword extraction fails, the bigrams still give recall, so the save never fails on search tokens alone.
- Bigrams add some false positives. Top-3 ranking plus the LLM answering step absorb that.
- The index is larger than the raw text. Negligible at single-household scale.

## Verification
- T1: test that `翡翠` finds `翡翠放紅櫃子`, written **red first** (plain `unicode61` over raw text), then green with `search_tokens`.
- T1: autogenerate against the migrated DB produces no FTS drop operations.
- T5: memory query injects ≤ 3 records into the prompt.

## Amendment (T5, 2026-11-22)
- `search_knowledge()` takes an optional `categories` filter. Memory queries pass `item_location`, so a sales copy that mentions 翡翠 never answers "翡翠放哪".
- `update_knowledge()` rewrites `search_tokens` in place. The existing `AFTER UPDATE OF search_tokens` trigger keeps the FTS5 copy in sync.
- Keywords for a photo save come from the Gemini call that already reads the photos. Text saves and queries use `TEXT_FAST_ROUTE`.
