# personal-assistant-for-May (Senior-Friendly Multi-Tenant AI Assistant)

## Team Roles & Operating Model
- **Product Owner (User)**: Defines goals, verifies features via plain-language `/teach` explanations, does NOT manually review code.
- **System Architect (Gemini)**: Owns `docs/ARCHITECTURE.md`, ADRs, and structural boundary decisions.
- **Local Dev Engineer (Claude Code)**: Implements tickets strictly per `docs/PHASE1_TASKS.md`, enforces TDD (`pstack /poteto-mode`), self-audits (`/interrogate`), and summarizes plainly (`/teach`).

## Environment Boundary (Cross-Platform Critical)
- **Local Dev Machine**: Windows 11 (`C:\Users\luke1\Desktop\brt\project\personal-assistant-for-May`), PowerShell, Docker Desktop.
- **Production Target**: Linux x86_64 home server running 24/7 via Docker Compose (`TZ=Asia/Taipei`).
- Always use `pathlib.Path` (no hardcoded `\` or `/tmp`), LF line endings, and cross-platform `python scripts/test.py`. Never rely on bash-only inline env syntax (`FOO=1 cmd`).
- **Time & paths (ADR-0005)**: Store UTC-aware via `UTCDateTime`; compute "Taipei today" with `ZoneInfo("Asia/Taipei")` → UTC range; inject `Clock`; never read process local time. DB stores media paths as POSIX strings relative to `DATA_DIR`; the domain passes `asset_id`, never `Path`.
- **Dev/Prod isolation**: Separate Telegram bot tokens for dev and prod. On Windows run natively (`uv run`); the `./data` bind mount is Linux-prod only (SQLite WAL is unreliable over Docker Desktop file sharing).

## Commands
- Install deps: `uv sync`
- Run offline guarded tests: `python scripts/test.py`
- Lint & typecheck: `uv run ruff check . && uv run mypy src`
- Local run: `uv run python -m src.main`
- Production container build: `docker compose up --build`

## Non-Obvious Architecture Rules
- **Single Runtime Core**: Routing, state machines, session lifecycle, and FinOps limits live strictly in Python (`src/domain/`, `src/pipeline/`). LLMs are stateless workers invoked only through `LLMPort`.
- **3-Layer Bounded Context (Zero Infinite Compaction)**:
  - Never keep an unbounded rolling chat log or run recursive prose compaction loops.
  - Stateless workflows (`lecture_digest`, `sales_copy`, `memory_stash`, `ios_helper`) run with **0 chat history**; on completion they save to `knowledge_items` and open a clean Layer-2 follow-up session holding only `{"last_artifact_id", "workflow"}` (ADR-0003).
  - Follow-up `topic_sessions` invariant after every commit: **`len(recent_turns) <= 6` AND history <= 1,500 tokens** (estimator `ceil(len * 1.2)`). Lazy auto-close after 15 mins idle, on topic shift, or on finish button. At most **one successful** Pydantic-validated <= 250-token slot extraction per session (`extraction_count <= 1`; a call with no response is not counted); a second ceiling hit force-closes.
  - Prompt budget is hard: text <= 4,500 tokens (system 500 / state_json 250 / retrieved_context 1,200 / recent_turns 1,500 / current_user_msg 1,050), multimodal <= 7,500 (<= 6 images at 560 tokens each via Gemini `MEDIA_RESOLUTION_MEDIUM`, voice <= 120 s at 32 tokens/s). Free text is truncated to 850 chars; every `recent_turns` message is truncated to <= 250 tokens. See `docs/ARCHITECTURE.md` §4.
  - Routing is deterministic first: budget 100% → fixed rest message (photo events skip this gate so a batch still gets one reply; its intent callback is gated); button callbacks & photo batches → 0 LLM; only free text calls DeepSeek, always with thinking mode disabled.
  - Voice is transcribed once (Gemini) and the transcript then takes the free-text route, Layer-2 stickiness included; a silent clip gets a fixed retry reply without DeepSeek. Voice > 120 s or files > 15 MB are rejected in the adapter before download.
  - Long-term memory lives in SQLite (`knowledge_items.search_tokens` + FTS5 top-3 retrieval, ADR-0004), never in prompt history.
- **Phase 1 External Dependency Boundary**: Phase 1 integrates ONLY Telegram (Long Polling), Gemini API direct, DeepSeek API direct, and Discord Webhook. Do NOT implement LINE, Cloudflare, or Google Calendar/Sheets code in Phase 1.
- **Telegram First, LINE-Ready Contract**: Workflows MUST ONLY return platform-agnostic `BotResponse` objects (max 3 bullets, max 4 buttons with labels <= 20 chars, `copyable_blocks` <= 1,000 chars, no Telegram-only features) so Phase 2 `LineAdapter` requires zero workflow rewrites.
  - **Single-response invariant (ADR-0002)**: one event/batch → at most one `BotResponse`. Progress = chat actions only; never send "處理中..." text. Adapters own reply-vs-push via `ReplyTarget`.
  - TelegramAdapter measures Telegram limits in UTF-16 (CopyTextButton 256, message 4,096, caption 1,024) and always renders one message; only allowlisted users in private chats get through.
- **Dual Direct LLM Providers (No OpenRouter)**: `gemini` (`google-genai`) for multimodal/audio/TW copywriting; `deepseek` (`openai` SDK) for fast text routing & keyword extraction. Model IDs come from `.env`; their prices live next to them in `src/config.py`, and an unknown model fails fast. Each Gemini model has a pinned thinking level plus thinking headroom above the answer cap; optional `GEMINI_FALLBACK_MODEL` is the same-provider spare tried once on 429/503/504 (multimodal included) before the cross-provider fallback.
- **Zero-Token Pre-Processing**: Pass all raw photo bursts through `batch_aggregator` + `image_gate` (blur + pHash dedup) before any LLM call.
- **Multi-Tenant Isolation**: Every DB table and repository query MUST require explicit `tenant_id` and `user_id`. A `users` row is a person; channel accounts live in `channel_identities` (ADR-0001). The only lookups without both are identity resolution `(channel, external_user_id)`, the startup-only orphan batch scan (`list_orphan_batches`), and tenant-level FinOps aggregates (ledger day sum, daily budget, 80% alert claim), which are still strictly scoped by `tenant_id`.
- **Dual-Track Sales Compliance**: Never refuse direct-sales copy; output Version A (compliant persuasive) + optional Version B (original intent + explicit Taiwan legal warning).

## Progressive Docs (Read On Demand)
- Full architecture, 3-Layer Context Law, External Services, Roadmap: `docs/ARCHITECTURE.md`
- Active sprint tickets & DoD: `docs/PHASE1_TASKS.md`
- Decisions: `docs/adr/` (0001 identity split · 0002 ReplyTarget & single response · 0003 session layers & prompt budget · 0004 CJK search · 0005 time & path normalization)
- Review trail: `docs/reviews/`
- Linux deploy & pre-travel runbook: `docs/DEPLOY_LINUX.md`

## Workflow Discipline (`pstack` Only)
- Read the active ticket in `docs/PHASE1_TASKS.md` before coding; stay strictly inside ticket scope.
- Reproduce bugs with a failing pytest test before fixing (`/poteto-mode`).
- Mock all external HTTP/API calls in tests; any real network call under `TEST_NO_SEND=1` must raise `NetworkBlockedError` (a `RuntimeError` subclass, `src/adapters/errors.py`). The guard lives in a `tests/conftest.py` autouse fixture (httpx and httpx2 transports, `socket.connect`, and `asyncio` `create_connection`, because the Windows proactor loop never calls `socket.connect`), so it holds even under plain `uv run pytest`.