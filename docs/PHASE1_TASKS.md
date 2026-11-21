# Phase 1 MVP Development Tickets (Telegram First, LINE-Ready Core)

> Rule: Execute ONE ticket per Claude Code session. Verify tests + `/interrogate` + `/teach`, commit cleanly (never commit `.env`), then run `/clear`.
> v1.3 (2026-11-21): Tickets updated per Architect rulings — see `docs/adr/0001`–`0005` and `docs/reviews/2026-11-21-architect-blind-spots.md`.
> v1.3.1 (2026-11-21): G1–G4 resolved (ADR-0003). Text budget 4,500, free text 850 chars, per-turn 250 tokens, `awaiting_intent` TTL 15 min.

## T1: Hexagonal Scaffold, Multi-Tenant DB & Cross-Platform Docker Base
- **Goal**: Establish Python 3.12 project (`uv`, `pydantic-settings`, `SQLAlchemy 2.0 async`, `aiosqlite`, `alembic`, `ruff`, `mypy`, `pytest`, `tzdata`) developing smoothly on Windows and running identically in Linux Docker.
- **Deliverables**:
  - `pyproject.toml`, `.python-version` (`3.12`), `.env.example` (documents separate dev/prod `TELEGRAM_BOT_TOKEN`; UTF-8 without BOM), `.gitignore` (strictly ignoring `.env`, `data/`, `secrets/`).
  - `Dockerfile` (Python 3.12 slim, exec-form `CMD`, non-root UID owning `/app/data`) and `docker-compose.yml` (`TZ=Asia/Taipei`, `init: true`, `restart: unless-stopped`, json-file log rotation, `./data:/app/data` for Linux prod; test runs use a container-local / named volume).
  - `tests/conftest.py`: autouse fixture forcing `APP_ENV=test` + `TEST_NO_SEND=1` and blocking any unmocked outbound network at the httpx-transport / socket layer (raises `RuntimeError`).
  - `src/domain/models.py` (`IncomingEvent` with `event_id`, `ReplyTarget`, `asset_ids`, `event_type` incl. `"unsupported"`, optional `group_index`/`group_total`; `BotResponse` with derived `alt_text`; `CopyableBlock` `body_text` ≤ 1,000 chars; `ActionButton` label ≤ 20 chars, `callback_data` ≤ 64 bytes) and `src/domain/ports.py` (`ChannelPort`, `LLMPort`, `StoragePort` incl. `search_knowledge()`, `OpsNotifierPort`, `Clock`).
  - `src/adapters/storage/`: `UTCDateTime` TypeDecorator; SQLAlchemy models + Alembic initial migration for all 8 tables (incl. `channel_identities` with `UNIQUE(channel, external_user_id)`, `topic_sessions.extraction_count` + partial unique index `WHERE status='active'`, `knowledge_items.search_tokens`, `tenants.budget_alert_80_date`); FTS5 virtual table + triggers over `search_tokens` created in a hand-written migration, with Alembic `include_object` excluding FTS internal tables; all queries scoped by `tenant_id` (identity resolution is the single documented exception).
  - `src/config.py` reading Phase 1 env vars (`GEMINI_API_KEY`, `GEMINI_MODEL`, `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`, `DISCORD_OPS_WEBHOOK_URL`, `DEFAULT_DAILY_BUDGET_USD`, `DATA_DIR`) plus a per-model price table; an unknown model ID fails fast at startup.
- **DoD**:
  - `python scripts/test.py` passes: migration; tenant-isolation CRUD; `channel_identities` uniqueness + one user with two channel identities sees the same `knowledge_items`; `UTCDateTime` round-trip returns UTC-aware values on SQLite; Taipei-day → UTC-range helper with a frozen `Clock`; Chinese 2-char search (`翡翠` finds `翡翠放紅櫃子`) written red first, then green; network guard raises on an unmocked httpx call even when run via plain `uv run pytest`.
  - `ruff` & `mypy` 0 errors; `docker compose config` validates cleanly.
  - `docker compose run --rm app python scripts/test.py` passes inside the Linux container.

## T2: Zero-Token Photo Burst Pipeline (`batch_aggregator` + `image_gate`)
- **Goal**: Protect LLM budget from rapid 10-30 photo bursts before any AI call happens.
- **Deliverables**:
  - Dependencies: `opencv-python-headless`, `imagehash`, `pillow-heif` (HEIC decode).
  - `src/pipeline/batch_aggregator.py`: Collects consecutive images sharing `media_group_id` OR arriving within configurable debounce window (default 5s; 0.05s in unit tests); closes early when `group_index == group_total` is known. Implements the `media_batches` state machine (ARCHITECTURE §4.6) and idempotent startup orphan recovery for `collecting` / `filtered` batches.
  - `src/pipeline/image_gate.py`: OpenCV Laplacian blur variance (`BLUR_THRESHOLD` configurable) + `imagehash` pHash dedup (hamming distance <= 6); keeps up to `MAX_BATCH_IMAGES=6` sharpest unique images; flags all-blurry batches with a warm retake prompt (batch → `done`). Accepts HEIC and image documents normalized by the adapter.
  - Produces a single `BotResponse` asking May to tap 1 of 3 intent buttons (`📚 整理講座重點` / `💎 寫珠寶推銷文案` / `📦 記下東西放哪了`) whose `callback_data` carries `batch_id`.
- **DoD**: Pytest simulates 15-photo burst (5 duplicates + 4 blurry) -> collapses into 1 DB batch, keeps only sharp unique photos, calls ZERO LLMs, returns exactly 1 valid `BotResponse` with <= 4 actions; HEIC fixture decodes; restart with an orphan `collecting` batch recovers it once (re-running is a no-op); repeated callback on a `done` batch is an idempotent no-op; a callback after the 15-minute `awaiting_intent` TTL (frozen `Clock`, lazy check on `intent_expires_at`) is rejected with a gentle resend prompt.

## T3: Dual Direct LLM Adapters, FinOps Guardian & 3-Layer Bounded Session Manager
- **Goal**: Connect Gemini & DeepSeek directly with hard budget circuit-breakers and zero-infinite-compaction session control.
- **Deliverables**:
  - `src/adapters/llm/gemini_adapter.py` (`google-genai` SDK: text + multi-image + native audio) and `src/adapters/llm/deepseek_adapter.py` (`openai` SDK targeting `https://api.deepseek.com`). Both record API-returned `usage` to the ledger.
  - `src/adapters/llm/router_llm.py`: Routes by `TaskCapability` (`MULTIMODAL_*`/`COPYWRITING_TW` -> Gemini; `TEXT_FAST_ROUTE` -> DeepSeek) with `HTTP 402/429` graceful handling.
  - `src/pipeline/finops_guardian.py`: Logs every call to `llm_usage_ledger` with cost from the config price table; Taipei day computed via `ZoneInfo` → UTC range; Discord Ops warning at 80% once per day (persisted in `tenants.budget_alert_80_date`); hard block at 100%.
  - `src/domain/router.py`: Implements the **3-Layer Bounded Context Law** (ARCHITECTURE §4):
    - Token estimator `ceil(len(text) * 1.2)`; prompt budget table enforced before every call (text ≤ 4,500 with slots system 500 / state_json 250 / retrieved_context 1,200 / recent_turns 1,500 / current_user_msg 1,050; multimodal ≤ 6,000; ≤ 6 images; voice ≤ 120 s rejected before LLM; free text deterministically truncated to 850 chars with a gentle notice; every `recent_turns` message truncated to ≤ 250 tokens).
    - Invariant after every commit: `len(recent_turns) <= 6` AND history ≤ 1,500 tokens. First ceiling hit → one Pydantic-validated slot extraction, deterministically truncated to ≤ 250 tokens, old turns dropped; `extraction_count <= 1`; second ceiling hit → force-close + "開新話題" prompt.
    - Routing priority: budget 100% → fixed rest message (0 LLM); button callbacks & photo batches → deterministic (0 LLM); free text → DeepSeek.
    - Lazy 15-min idle close; instant reset on topic switch.
    - Per-user `asyncio.Lock` + reliance on the partial unique index for single active session.
  - `src/adapters/discord_ops/notifier.py`: Async webhook sender (blocked by the global conftest network guard in tests).
- **DoD**: Mocked tests prove: (a) routing sends image/audio to Gemini & intent/slots to DeepSeek; (b) 100% daily budget blocks LLM & notifies Discord mock, and routing makes 0 LLM calls; (c) the 4th user message triggers exactly one slot extraction and drops old turns, `recent_turns` never exceeds 6 messages / 1,500 tokens, and a second ceiling hit force-closes (no second extraction); (d) topic shift or 15m idle (frozen `Clock`) yields a clean 0-history session; (e) unmocked HTTP raises; (f) 80% alert is sent once per Taipei day across a simulated restart; (g) a callback event makes 0 LLM calls; (h) two concurrent events for one user never create two active sessions; (i) unknown model ID fails at config load; (j) 121 s audio is rejected before any LLM call.

## T4: Core Workflows — Lecture Digest & Dual-Track Sales Copy
- **Goal**: Deliver May's two highest-impact daily workflows (both running as Layer-1 stateless 0-history workflows by default).
- **Deliverables**:
  - `src/workflows/lecture_digest.py`: Takes kept images from a `media_batch` -> Gemini multimodal (0 prior chat history) -> `BotResponse` (3 plain-Mandarin takeaways + "這場講座的目的/溫馨提醒") -> archives to `knowledge_items` -> opens a clean Layer-2 follow-up session (`recent_turns=[]`, `state_json={"last_artifact_id", "workflow"}`).
  - `src/domain/compliance.py` + `data/tw_prohibited_terms.yaml`: Deterministic Taiwan direct-sales medical claim keyword scanner.
  - `src/workflows/sales_copy.py`: Takes 1-3 jewelry/product photos + optional voice/text note -> saves to `knowledge_items` -> returns `BotResponse` with `CopyableBlock` Version A (compliant persuasive, verified 0 prohibited terms) and optional Version B (original intent + legal risk warning), each `body_text` ≤ 1,000 chars. Supports follow-up tone tweaks inside the bounded Layer-2 session opened on completion.
- **DoD**: E2E mocked tests pass for both workflows; deterministic compliance scanner catches prohibited terms even if mocked LLM output leaks them; verifies 0 history injection on fresh workflow starts; verifies Layer-1 completion opens exactly one active Layer-2 session holding only `last_artifact_id` + `workflow` (no artifact text in `state_json`); each workflow returns exactly 1 `BotResponse`.

## T5: Memory Stash ("東西放哪了"), iOS 3-Step Helper & TelegramAdapter Integration
- **Goal**: Wire everything together into a working Telegram Bot ready for Linux Docker deployment.
- **Deliverables**:
  - `src/workflows/memory_stash.py`: Photo + voice/text ("翡翠放紅櫃子") saves to `knowledge_items` with `search_tokens` (DeepSeek keywords + Python char bigrams); voice/text query extracts keywords via DeepSeek -> FTS5 top-3 retrieval over `search_tokens` (Layer-3 memory, shared `retrieved_context` ≤ 1,200 tokens) -> returns `BotResponse` with matching `image_asset_id` + location note.
  - `src/workflows/ios_helper.py`: Voice or screenshot question about iPhone -> returns 3-step large-clarity `BotResponse`.
  - `src/adapters/telegram/adapter.py` (`python-telegram-bot` v21+ async Long Polling):
    - Enforces strict allowlist (`TELEGRAM_ALLOWED_USER_IDS`) rejecting unknown users silently or cleanly; resolves `channel_identities` → internal `tenant_id`/`user_id`.
    - Builds `IncomingEvent` with `event_id` (`update_id`, dedup before side effects), `ReplyTarget(address=chat_id, ephemeral_token=None)`; normalizes image documents/HEIC to `"image"`; maps stickers/video/other files to `"unsupported"`.
    - Downloads voice `.ogg` / photos and persists originals locally via `StoragePort` (POSIX path relative to `DATA_DIR`; `external_file_id` cache only) before emitting `asset_ids`.
    - Sends only chat actions (`typing` / `upload_photo`) as progress — never intermediate text messages; sends at most 1 `BotResponse` per event/batch.
    - Renders `BotResponse`: HTML-escaped text; `CopyTextButton` for `body_text` ≤ 256 chars, `<pre>` block for 257–1,000; InlineKeyboard buttons.
  - `src/main.py`: Bootstraps DB migrations, runs `media_batches` orphan recovery, wires adapters; graceful shutdown via PTB `run_polling()` (no custom signal handlers).
  - `docs/DEPLOY_LINUX.md`: Concise checklist — `.env` setup (separate prod bot token, UTF-8 no BOM), non-root UID / `./data` permissions, `docker compose up -d`, `restart: unless-stopped`, log rotation, scheduled `sqlite3 .backup`.
- **DoD**: Full mocked integration tests pass; allowlist rejects unknown Telegram user IDs; FTS5 retrieval injects <= 3 records into prompt; CopyTextButton/`<pre>` boundary tests at 255/256/257 chars; `unsupported` event returns the fixed gentle reply; a re-delivered `update_id` is processed once; no handler sends more than 1 message per event; `python scripts/test.py`, `ruff`, `mypy` all green; `docker compose run --rm app python scripts/test.py` passes inside the Linux container.
