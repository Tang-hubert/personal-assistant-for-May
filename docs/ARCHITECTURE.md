# System Architecture Specification (v1.3.1)

> v1.3 (2026-11-21): Applies Architect rulings on the `/architect` blind-spot review. Rationale lives in `docs/adr/0001`–`0005`; findings and rulings trace in `docs/reviews/2026-11-21-architect-blind-spots.md`.
> v1.3.1 (2026-11-21): Resolves G1–G4 (prompt budget closure, per-turn truncation, `awaiting_intent` TTL). See ADR-0003.

## 1. High-Level Topology

```text
[May's iPhone]                                  [Admin Ops - Discord]
  Phase 1: Telegram App (Dedicated Bot)                   ▲ (80%/100% Budget Alerts, Errors)
  Phase 2: LINE OA (+ iOS Shortcut Icon)                  │
        │ (Voice / Photo Burst / Tap)                     │
        ▼                                                 │
┌─────────────────────────────────────────────────────────┴────────────┐
│         Docker Container (Linux Prod / Windows Dev - Python 3.12)    │
│                                                                      │
│  [1. Channel Adapter Layer]  (implements ChannelPort)                │
│     • Phase 1: TelegramAdapter (Long Polling, media_group, allowlist)│
│     • Phase 2: LineAdapter (Flex + ClipboardAction + Reply/Push)     │
│         ▼ (Normalized IncomingEvent)                                 │
│  [2. Pre-Processor Pipeline (0 Token)]                               │
│     • Burst Aggregator: group_id + 5s debounce window                │
│     • Image Gate: OpenCV Laplacian blur + imagehash pHash dedup      │
│         ▼                                                            │
│  [3. FinOps Guardian & Bounded Context Manager]                      │
│     • Hard daily USD cap (Prepaid + App Circuit Breaker)             │
│     • Intent Router (deterministic first, DeepSeek for free text)    │
│     • 3-Layer Bounded Session Control + Prompt Budget                │
│         ▼                                                            │
│  [4. Domain Workflows (100% Channel-Agnostic -> BotResponse)]        │
│     • Lecture Digest  |  Sales Copy (Dual-Track Compliance)          │
│     • Memory Stash    |  iOS 3-Step Tech Helper                      │
│         ▼                                                            │
│  [5. Infrastructure Adapters]                                        │
│     • LLMPort: GeminiDirectAdapter + DeepSeekDirectAdapter           │
│     • StoragePort: SQLAlchemy Async (SQLite WAL + FTS5 -> Postgres)  │
│     • DiscordOpsAdapter: Async Webhook Alerts                        │
└──────────────────────────────────────────────────────────────────────┘
```

## 2. Channel-Agnostic Contract (Guaranteeing Seamless Telegram -> LINE Migration)
To prevent feature lock-in to Telegram, `src/domain/models.py` enforces strict LCD (Lowest Common Denominator) contracts. Every limit below is a Pydantic validator, not a convention.

### Normalized `IncomingEvent`
- `event_id: str` — idempotency key (Telegram `update_id`, LINE `webhookEventId`). Processing dedups on it before any side effect.
- `tenant_id: str`, `user_id: str` — internal IDs resolved from `channel_identities` (ADR-0001). Workflows never see channel user IDs.
- `channel: Literal["telegram", "line"]`
- `event_type: Literal["text", "audio", "image", "action_callback", "unsupported"]`
  - Image documents (iPhone "send as file", incl. HEIC) are normalized to `"image"` by the adapter.
  - Stickers, video, location, and other files map to `"unsupported"` and get one fixed, gentle reply.
- `media_group_id: str | None` (Telegram `media_group_id` / LINE `imageSet.id`), optional `group_index: int | None`, `group_total: int | None` (LINE `imageSet` provides both; Telegram does not).
- `reply_target: ReplyTarget` (ADR-0002) — `address: str` (persistent: Telegram `chat_id` / LINE `userId`), `ephemeral_token: str | None` (LINE `replyToken`; `None` on Telegram), `token_expires_at: datetime | None`. Only adapters read it; adapters choose reply vs push.
- `payload_text: str | None`. The contract does not truncate it. The pipeline deterministically truncates any free text to **850 chars** before any LLM call and adds a gentle notice (ADR-0003).
- `callback_data: str | None`. The button token of an `action_callback` event (required for that type).
- `asset_ids: list[str]` — media is downloaded and persisted by the adapter through `StoragePort` before the event enters the pipeline. No filesystem `Path` crosses the domain boundary (ADR-0005).

### Normalized `BotResponse` (Max Constraints Matched to the Stricter Channel)
- `title: str` (Max 30 chars)
- `bullets: list[str]` (Max 3 concise plain-Mandarin points, senior-readable)
- `alt_text: str` — derived from `title + bullets`, truncated to 400 chars (LINE Flex `altText` requirement). Not hand-written by workflows.
- `copyable_blocks: list[CopyableBlock]` (Each has `label`, `body_text` max **1,000** chars, optional `warning_note`)
  - *TelegramAdapter*: `body_text` ≤ 256 chars → `CopyTextButton`; 257–1,000 chars → HTML-escaped `<pre>` block (tap-to-copy).
  - *LineAdapter (Phase 2)*: Flex Bubble with native `ClipboardAction`.
- `actions: list[ActionButton]` (Max 4 buttons; each has `label` max 20 chars, `callback_data` as a short server-side token such as `b:<batch_id>:lecture` (≤ 64 bytes), optional `prefer_voice_input: bool`)
  - *TelegramAdapter* renders as InlineKeyboardMarkup.
  - *LineAdapter (Phase 2)* renders as QuickReply/Flex Postback (`inputOption: "openVoice"` when `prefer_voice_input=True`).
- `image_asset_id: str | None` (Max 1 primary image per reply)
- `priority: Literal["reply", "proactive_critical", "proactive_digest"]`

### Single-Response Invariant (ADR-0002)
- One `IncomingEvent` (or one media batch) produces **at most one** `BotResponse`.
- Progress feedback uses chat actions only (Telegram `typing` / `upload_photo`, LINE loading animation). Intermediate text messages such as "處理中..." are forbidden, because on LINE they would consume the single-use reply token.
- Adapters escape all LLM/user text for the target markup (Telegram HTML).

## 3. Dual Direct LLM Routing (`src/adapters/llm/`)
No third-party aggregators (no OpenRouter). Direct prepaid SDKs only (auto-recharge disabled):

| Task Type (`TaskCapability`) | Primary Provider | Fallback / Degrade | Notes |
| :--- | :--- | :--- | :--- |
| `MULTIMODAL_IMAGE` (Lecture / Jewelry / Stash) | `gemini` (`GEMINI_MODEL`) | Error + Notify Ops | Native multi-image understanding |
| `MULTIMODAL_AUDIO` (Voice notes in Mandarin) | `gemini` (`GEMINI_MODEL`) | Error + Notify Ops | Native audio input, skips local Whisper |
| `COPYWRITING_TW` (Sales copy, iOS guide) | `gemini` (`GEMINI_MODEL`) | `deepseek` | Best Taiwan Traditional Chinese tone |
| `TEXT_FAST_ROUTE` (Intent routing, slot extract, keywords) | `deepseek` (`DEEPSEEK_MODEL`) | `gemini` | Ultra-low cost fast text processing |

- Handle `HTTP 402` (Prepaid balance exhausted) and `HTTP 429` cleanly: alert Discord Ops and reply to user gently.
- **Price table co-located with model IDs**: `src/config.py` holds per-model USD prices next to the model ID settings. A configured model ID with no price entry fails fast at startup.

## 4. 3-Layer Bounded Context & Session Control (Eliminating Infinite Compaction)
Every LLM call MUST stay within a fixed prompt budget regardless of how many months the user uses the bot (ADR-0003).

### 4.1 Token Estimation
- Estimator: `ceil(len(text) * 1.2)` — deliberately over-estimates CJK.
- Real `usage` returned by each API is written to `llm_usage_ledger` and used to calibrate the ratio.

### 4.2 Prompt Budget Table (hard limits, enforced before every call)
| Call kind | Total | Slots |
| :--- | :--- | :--- |
| Text / follow-up | **≤ 4,500** tokens | system ≤ 500, state_json ≤ 250, retrieved_context ≤ 1,200, recent_turns ≤ 1,500, current_user_msg ≤ 1,050 |
| Multimodal | **≤ 6,000** tokens | ≤ 6 images; voice ≤ 120 s (longer audio is rejected gently before any LLM call); free text ≤ 850 chars |

- `retrieved_context` is one shared slot. A follow-up puts the truncated original text of `last_artifact_id` there; a memory query puts the Layer-3 top-3 records there. Never both.
- Free text over 850 chars is deterministically truncated (850 × 1.2 = 1,020 ≤ 1,050) and the reply gently says so. This applies to every free-text input, captions included.

### 4.3 The Three Layers
1. **Layer 1 — Stateless One-Shot Workflows (0 History Tokens)**:
   - `lecture_digest`, `sales_copy` (initial draft), `memory_stash` (save), and `ios_helper` (initial question) inject **zero prior conversation history**.
   - On completion the result is saved to `knowledge_items`, then a **clean Layer-2 follow-up session is opened (or reset)** with `recent_turns = []` and `state_json = {"last_artifact_id": "...", "workflow": "..."}` — a reference, never the artifact text.
2. **Layer 2 — Hard-Bounded Active Topic Session (`topic_sessions`)**:
   - Used ONLY for immediate follow-up tweaks (e.g., "把剛剛的文案改得更親切一點").
   - **Invariant after every commit**: `len(recent_turns) <= 6` (3 user + 3 assistant) AND history ≤ 1,500 estimated tokens.
   - **Per-turn cap**: every message written to `recent_turns` is deterministically truncated to ≤ 250 tokens (6 × 250 = 1,500). Full long outputs (copy, reports) live only in `knowledge_items` and reach the prompt through `state_json.last_artifact_id` → `retrieved_context`. They are never duplicated into `recent_turns`.
   - **Auto-Close Triggers**: (a) idle > 15 minutes — evaluated lazily when the next message arrives (no scheduler); (b) `TEXT_FAST_ROUTE` detects topic change; (c) user taps the finish button.
   - **Single extraction, no recursive compaction**: when the ceiling is hit the first time, extract a fixed-schema slot dict via `TEXT_FAST_ROUTE`, validate it with a Pydantic schema, deterministically truncate it to ≤ 250 tokens (never send it back to the LLM to rewrite), store it in `state_json`, and hard-drop all older raw turns. `extraction_count <= 1`: hitting the ceiling a second time force-closes the session and invites May to start a new topic.
3. **Layer 3 — Externalized Long-Term Memory (SQLite Retrieval)**:
   - Historical items live in `knowledge_items`, searched through `search_tokens` + FTS5 (ADR-0004).
   - Memory queries extract keywords via `TEXT_FAST_ROUTE` and inject **at most Top 3 matching records (≤ 1,200 tokens, the shared `retrieved_context` slot)**.

### 4.4 Routing Priority (deterministic first)
1. FinOps at 100% → skip routing entirely, return the fixed rest message (0 LLM).
2. Button callbacks and photo batches → deterministic routing (0 LLM).
3. Free text only → `TEXT_FAST_ROUTE` (DeepSeek) for intent / topic-shift detection.

### 4.5 Concurrency
- Single-process assumption for Phase 1.
- DB partial unique index on `topic_sessions(tenant_id, user_id) WHERE status = 'active'` — at most one active session per user (SQLite and Postgres both support partial indexes).
- In-process per-user `asyncio.Lock` serializes handling of one user's events.

### 4.6 `media_batches` State Machine
| From | Trigger | To |
| :--- | :--- | :--- |
| (new) | first image of a group / debounce window | `collecting` |
| `collecting` | debounce elapsed (or `group_index == group_total`) | `filtered` (image gate run) |
| `filtered` | ≥ 1 kept image → intent-button `BotResponse` sent | `awaiting_intent` |
| `filtered` | all images blurry → warm retake `BotResponse` sent | `done` |
| `awaiting_intent` | intent callback → workflow completes | `done` |
| `done` | repeated callback | `done` (idempotent no-op, gentle "已經處理好囉") |

- A new burst arriving while another batch is `awaiting_intent` opens a new batch; callbacks carry `batch_id`, so old buttons stay valid.
- **Startup orphan recovery (idempotent)**: batches left in `collecting` / `filtered` by a restart re-run the image gate over their persisted assets and proceed per the table, sending that batch's single response once.
- **`awaiting_intent` TTL (G4)**: 15 minutes (same as the session idle window), checked lazily against `media_batches.intent_expires_at` when a callback arrives. `callback_data` binds `batch_id`. Callbacks on a `done` or expired batch are idempotently rejected with a gentle resend prompt.

## 5. Core Database Schema (SQLite WAL + FTS5 -> Postgres Portable)
- Every table MUST contain `tenant_id TEXT NOT NULL` and `created_at`.
- **All timestamp columns use the custom `UTCDateTime` TypeDecorator** (UTC-aware on write and read, on SQLite and Postgres alike — ADR-0005).
- Every repository query requires explicit `tenant_id` and `user_id`. The single documented exception is identity resolution `(channel, external_user_id) -> (tenant_id, user_id)`, which is how both IDs are obtained.

1. `tenants`: `id`, `name`, `plan_tier`, `daily_budget_usd` (default `0.20`), `budget_alert_80_date` (Asia/Taipei date of the last 80% alert; persisted so restarts never resend), `config_json`
2. `users` (a person): `id`, `tenant_id`, `display_name`, `preferences_json`
3. `channel_identities`: `id`, `tenant_id`, `user_id`, `channel` (`telegram|line`), `external_user_id`, `UNIQUE(channel, external_user_id)` (ADR-0001)
4. `topic_sessions`: `id`, `tenant_id`, `user_id`, `topic_type` (`lecture|sales|memory|ios_help|general`), `status` (`active|closed`), `turn_count`, `extraction_count`, `state_json`, `recent_turns_json`, `last_active_at` + partial unique index (§4.5)
5. `media_batches`: `id`, `tenant_id`, `user_id`, `channel`, `media_group_id`, `status` (`collecting|filtered|awaiting_intent|done`), `raw_count`, `kept_count`
6. `media_assets`: `id`, `batch_id`, `tenant_id`, `external_file_id` (cache only), `media_type`, `local_path` (POSIX string relative to `DATA_DIR`), `phash`, `blur_score`, `is_kept`, `is_blurry`
   - **Invariant**: every asset keeps its original file locally. `external_file_id` is never the only copy (Phase 2 LINE needs our own bytes).
7. `knowledge_items`: `id`, `tenant_id`, `user_id`, `category` (`jewelry|product|lecture|item_location|crm`), `title`, `content_text`, `structured_json`, `primary_asset_id`, `search_tokens` (+ FTS5 virtual table over `search_tokens`, ADR-0004)
8. `llm_usage_ledger`: `id`, `tenant_id`, `user_id`, `provider`, `model`, `prompt_tokens`, `completion_tokens`, `estimated_cost_usd`, `workflow_name`

## 6. FinOps Guardian & Dual-Track Taiwan Direct-Sales Compliance
- **FinOps**: Before EVERY LLM call, sum the tenant's ledger for the current Asia/Taipei day. The day boundary is computed in Python with `ZoneInfo("Asia/Taipei")` and converted to a UTC range for the query — never from process local time (ADR-0005). `>= 80%`: warn Discord Ops once per day (guarded by `tenants.budget_alert_80_date`). `>= 100%`: block LLM, return friendly rest message, alert Discord Ops.
- **Test isolation**: `tests/conftest.py` autouse fixture forces `APP_ENV=test` + `TEST_NO_SEND=1` and blocks unmocked outbound network at the httpx-transport / socket layer, so the guard holds even when `pytest` runs without `scripts/test.py`.
- **Compliance (`src/domain/compliance.py`)**: Deterministic scanner using `data/tw_prohibited_terms.yaml`. Always generate Version A (safe persuasive experience framing, 0 prohibited terms verified post-LLM) + optional Version B (polished original intent with explicit legal risk `warning_note`).

## 7. External Services Matrix (Strict Phase Boundaries)
- **Phase 1 Runtime (4 APIs Only)**: Telegram Bot API (Long Polling), Google AI Studio Gemini API (Prepaid), DeepSeek API (Prepaid), Discord Ops Webhook.
- **Phase 1 Deployment Ops (2 Tools)**: Private GitHub Repo, Tailscale Mesh VPN.
- **Deferred to Phase 2 (DO NOT build in Phase 1)**: LINE Messaging API OA, Cloudflare Worker/Tunnel/R2, Google Cloud (Calendar & Sheets APIs).

## 8. Product Roadmap (Phase 1 -> Phase 3)
- **Phase 1 (Current MVP — Telegram)**: Photo burst gate + Lecture Digest + Dual-Track Jewelry/Sales Copy + "Where Did I Put It" Memory Stash + iOS 3-Step Helper.
- **Phase 2 (LINE Migration & Ecosystem)**: Add `LineAdapter` (+ one `channel_identities` row per user to link LINE) + iPhone Home Shortcut + Cloudflare Edge Fallback; Google Calendar native iOS reminders; Voice CRM + one-way Google Sheets sync; Health fact-checking (Gemini Grounding) + TTS voice replies.
- **Phase 3 (Taiwan SaaS Productization)**: Swap `DATABASE_URL` to Cloud PostgreSQL (with a Postgres implementation of `StoragePort.search_knowledge()`); multi-tenant self-onboarding & billing; cloud container hosting.

## 9. Cross-Platform & Deployment Rules
- **Separate bots**: dev and prod MUST use different `TELEGRAM_BOT_TOKEN`s. Two long-polling processes on one token cause `409 Conflict` and dropped messages.
- **Python pinned**: `.python-version` = `3.12`; Docker base image uses the same minor.
- **Dependencies**: `opencv-python-headless` (no libGL in slim images), `pillow-heif` (iPhone HEIC), `tzdata` (IANA zones on Windows and slim images).
- **Time & paths** (ADR-0005): UTC-aware storage via `UTCDateTime`; injected `Clock`; DB stores media paths as POSIX strings relative to `DATA_DIR`.
- **Container verification**: `docker compose run --rm app python scripts/test.py` is part of T1 and T5 DoD. It uses a container-local / named volume, never the Windows bind mount (SQLite WAL is unreliable over Docker Desktop file sharing).
- **Volumes**: `./data:/app/data` bind mount is for Linux prod only. On Windows, run the app natively with `uv run`; never open one SQLite file from a native process and a container at the same time.
- **Container runtime**: exec-form `CMD`, `init: true`, non-root UID owning `/app/data`, `restart: unless-stopped`, json-file log rotation (`max-size` / `max-file`).
- **Shutdown**: graceful shutdown is delegated to PTB `run_polling()`; no custom `loop.add_signal_handler` (unsupported on Windows).
- **Ops**: scheduled `sqlite3 .backup` of the DB; `.env` must be UTF-8 without BOM.
