# System Architecture Specification (v1.3.4)

> v1.3 (2026-11-21): Applies Architect rulings on the `/architect` blind-spot review. Rationale lives in `docs/adr/0001`–`0005`; findings and rulings trace in `docs/reviews/2026-11-21-architect-blind-spots.md`.
> v1.3.1 (2026-11-21): Resolves G1–G4 (prompt budget closure, per-turn truncation, `awaiting_intent` TTL). See ADR-0003.
> v1.3.2 (2026-11-21): T3 rulings D1–D6. Multimodal budget 7,500 with media token estimates, `audio_duration_s`, photo events bypass the budget gate, tenant-level FinOps aggregate exception, 402/429 classification, DeepSeek thinking disabled. See ADR-0003 "Amendment (T3)".
> v1.3.4 (2026-11-22): T5 rulings Q1–Q8. Voice is transcribed and then routed like typed text; a 4th intent button for iPhone screenshots; `ios_guide` knowledge category; memory search filters by category and updates locations in place; Telegram limits measured in UTF-16; 15 MB pre-download cap; idempotent account seed. See ADR-0002 and ADR-0003 "Amendment (T5)".

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
- `event_id: str` — idempotency key (Telegram `tg:u:<update_id>` for messages and `tg:cb:<callback_query.id>` for button taps, LINE `webhookEventId`). The adapter claims it in an in-process bounded set (4,096 ids) before any side effect, download included. A crash can still redeliver the few updates Telegram had not yet acknowledged (accepted, Q5).
- `tenant_id: str`, `user_id: str` — internal IDs resolved from `channel_identities` (ADR-0001). Workflows never see channel user IDs.
- `channel: Literal["telegram", "line"]`
- `event_type: Literal["text", "audio", "image", "action_callback", "unsupported"]`
  - Image documents (iPhone "send as file", incl. HEIC) are normalized to `"image"` by the adapter.
  - Stickers, video, location, and other files map to `"unsupported"` and get one fixed, gentle reply.
  - Pre-download gates (0 download, 0 LLM, one fixed reply): voice over 120 s or under 1 s (`replies.voice_rejection`, shared with the router), and any photo, document, or voice whose platform `file_size` exceeds 15 MB. A photo that is too large or whose download fails never reaches the aggregator, so one burst could produce many such replies: photo notices (`file_too_large` or `photo_not_received`) are limited to one per chat per 60 s, measured with the injected `Clock`. Ops gets one alert (exception type only) with each notice, not one per failed photo.
- `media_group_id: str | None` (Telegram `media_group_id` / LINE `imageSet.id`), optional `group_index: int | None`, `group_total: int | None` (LINE `imageSet` provides both; Telegram does not).
- `reply_target: ReplyTarget` (ADR-0002) — `address: str` (persistent: Telegram `chat_id` / LINE `userId`), `ephemeral_token: str | None` (LINE `replyToken`; `None` on Telegram), `token_expires_at: datetime | None`. Only adapters read it; adapters choose reply vs push.
- `payload_text: str | None`. The contract does not truncate it. The pipeline deterministically truncates any free text to **850 chars** before any LLM call and adds a gentle notice (ADR-0003).
- `callback_data: str | None`. The button token of an `action_callback` event (required for that type).
- `asset_ids: list[str]` — media is downloaded and persisted by the adapter through `StoragePort` before the event enters the pipeline. No filesystem `Path` crosses the domain boundary (ADR-0005).
- `audio_duration_s: float | None` — required and > 0 for `"audio"` events. The adapter fills it from platform metadata (Telegram `voice.duration`, LINE audio `duration`), so voice over 120 s is rejected before any LLM call without decoding the file.

### Normalized `BotResponse` (Max Constraints Matched to the Stricter Channel)
- `title: str` (Max 30 chars)
- `bullets: list[str]` (Max 3 concise plain-Mandarin points, senior-readable)
- `alt_text: str` — derived from `title + bullets`, truncated to 400 chars (LINE Flex `altText` requirement). Not hand-written by workflows.
- `copyable_blocks: list[CopyableBlock]` (Each has `label`, `body_text` max **1,000** chars, optional `warning_note`)
  - *TelegramAdapter*: `body_text` ≤ 256 chars → `CopyTextButton`; 257–1,000 chars → HTML-escaped `<pre>` block (tap-to-copy). Telegram counts in UTF-16 code units, so the adapter measures UTF-16 length (an emoji counts 2).
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
- One `BotResponse` renders as exactly one Telegram message: text ≤ 4,096 and photo caption ≤ 1,024 (UTF-16, measured on the escaped HTML). On overflow the adapter shortens the raw text of the longest block, then the bullets, and escapes again, so an HTML entity is never split.
- Telegram accepts only allowlisted users (`TELEGRAM_ALLOWED_USER_IDS`) in private chats. Strangers are ignored without a reply; the log records only their numeric ID. Because chats are private, `chat_id` equals the user ID, which lets `ChannelPort.send` resolve the owner to read `image_asset_id`.

## 3. Dual Direct LLM Routing (`src/adapters/llm/`)
No third-party aggregators (no OpenRouter). Direct prepaid SDKs only (auto-recharge disabled):

| Task Type (`TaskCapability`) | Primary Provider | Fallback / Degrade | Notes |
| :--- | :--- | :--- | :--- |
| `MULTIMODAL_IMAGE` (Lecture / Jewelry / Stash) | `gemini` (`GEMINI_MODEL`) | Error + Notify Ops | Native multi-image understanding |
| `MULTIMODAL_AUDIO` (Voice notes in Mandarin) | `gemini` (`GEMINI_MODEL`) | Error + Notify Ops | Native audio input, skips local Whisper |
| `COPYWRITING_TW` (Sales copy, iOS guide) | `gemini` (`GEMINI_MODEL`) | `deepseek` | Best Taiwan Traditional Chinese tone |
| `TEXT_FAST_ROUTE` (Intent routing, slot extract, keywords) | `deepseek` (`DEEPSEEK_MODEL`) | `gemini` | Ultra-low cost fast text processing |

- Handle `HTTP 402` (Prepaid balance exhausted) and `HTTP 429` cleanly: alert Discord Ops and reply to user gently.
  - Classification: `402` → balance exhausted (Ops `critical`). Gemini `429` whose message mentions depleted prepayment credits is also balance exhausted (Google's behavior before it switched to 402). Any other `429` → rate limited (Ops `warning`). `503`/`504` → overloaded (Ops `warning`). Other `5xx`, timeouts, connection errors, and unusable replies → provider error. The same (provider, reason) alert is throttled to once per 15 minutes.
  - Fallback order (T5.1): a `rate_limited` / `overloaded` primary is retried once on the same provider's spare model (`GEMINI_FALLBACK_MODEL`), images and audio included. Then the cross-provider fallback per the table, which applies only to text-only requests. Every extra attempt passes the budget gate again and is billed at the model that answered. `MULTIMODAL_*` never falls back across providers.
- **Gemini thinking (T5.1)**: each Gemini model has a pinned thinking level, and its thinking headroom is added on top of the workflow's `max_output_tokens`, which sizes the answer alone. Gemini counts thinking against that cap, and the default dynamic level cut short JSON replies.
- **DeepSeek thinking mode is disabled explicitly** on every call (`extra_body={"thinking": {"type": "disabled"}}`). DeepSeek enables thinking by default, which would add latency and billed output tokens to `TEXT_FAST_ROUTE`.
- `src/adapters/llm/router_llm.py` (`RoutingLLM`) is the single `LLMPort` every caller uses. Per call it runs the budget gate, checks the prompt budget, picks the provider, and writes API-returned `usage` to `llm_usage_ledger` before parsing the reply. Gemini thinking tokens count as output tokens.
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
| Multimodal | **≤ 7,500** tokens | ≤ 6 images; voice ≤ 120 s (longer audio is rejected gently before any LLM call); free text ≤ 850 chars |

- Media is estimated before dispatch at **560 tokens per image** (Gemini `MEDIA_RESOLUTION_MEDIUM`, fixed by the adapter) and **32 tokens per audio second** (conservative; Gemini 3 bills 25). Worst Phase-1 case: 3 images + 120 s voice + system 500 + user message 1,050 = 7,070 ≤ 7,500. The ledger's real `usage` calibrates these numbers.

- `retrieved_context` is one shared slot. A follow-up puts the truncated original text of `last_artifact_id` there; a memory query puts the Layer-3 top-3 records there. Never both.
- Free text over 850 chars is deterministically truncated (850 × 1.2 = 1,020 ≤ 1,050) and the reply gently says so. This applies to every free-text input, captions included.

### 4.3 The Three Layers
1. **Layer 1 — Stateless One-Shot Workflows (0 History Tokens)**:
   - `lecture_digest`, `sales_copy` (initial draft), `memory_stash` (save), and `ios_helper` (initial question) inject **zero prior conversation history**.
   - Photo batches offer four intent buttons: `📚 整理講座重點`, `💎 寫珠寶推銷文案`, `📦 記下東西放哪了`, `📱 教我用手機` (iPhone screenshots → `ios_helper`).
   - On completion the result is saved to `knowledge_items`, then a **clean Layer-2 follow-up session is opened (or reset)** with `recent_turns = []` and `state_json = {"last_artifact_id": "...", "workflow": "..."}` — a reference, never the artifact text.
2. **Layer 2 — Hard-Bounded Active Topic Session (`topic_sessions`)**:
   - Used ONLY for immediate follow-up tweaks (e.g., "把剛剛的文案改得更親切一點").
   - **Follow-up stickiness**: while the session holds `last_artifact_id` and the classifier reports no topic shift, the message goes to the handler of the session's `topic_type`, whatever route the classifier named. A rewrite saves a new `knowledge_items` row and the router moves `last_artifact_id` to it (`HandlerReply.last_artifact_id`), so the next tweak builds on the latest version. The same commit sets the session's `topic_type` to the route that produced the artifact, so a memory or iOS answer given inside a session that "你好" opened as `general` keeps its follow-ups.
   - **Invariant after every commit**: `len(recent_turns) <= 6` (3 user + 3 assistant) AND history ≤ 1,500 estimated tokens.
   - **Per-turn cap**: every message written to `recent_turns` is deterministically truncated to ≤ 250 tokens (6 × 250 = 1,500). Full long outputs (copy, reports) live only in `knowledge_items` and reach the prompt through `state_json.last_artifact_id` → `retrieved_context`. They are never duplicated into `recent_turns`.
   - **Auto-Close Triggers**: (a) idle > 15 minutes — evaluated lazily when the next message arrives (no scheduler); (b) `TEXT_FAST_ROUTE` detects topic change; (c) user taps the finish button.
   - **Single extraction, no recursive compaction**: when the ceiling is hit the first time, extract a fixed-schema slot dict via `TEXT_FAST_ROUTE`, validate it with a Pydantic schema, deterministically truncate it to ≤ 250 tokens (never send it back to the LLM to rewrite), store it in `state_json`, and hard-drop all older raw turns. `extraction_count <= 1` counts successful extractions; a call that got no response (providers down) is not counted and may be retried on the next message. Hitting the ceiling again after a successful extraction force-closes the session with the fixed new-topic reply (no handler call; the message still pays its routing classification, so a real topic shift escapes the force-close).
3. **Layer 3 — Externalized Long-Term Memory (SQLite Retrieval)**:
   - Historical items live in `knowledge_items`, searched through `search_tokens` + FTS5 (ADR-0004).
   - Memory queries extract keywords via `TEXT_FAST_ROUTE` and inject **at most Top 3 matching records (≤ 1,200 tokens, the shared `retrieved_context` slot)**.

### 4.4 Routing Priority (deterministic first)
1. FinOps at 100% → skip routing entirely, return the fixed rest message (0 LLM).
   - Exception: photo events skip the budget gate and are still collected into a batch that gets its intent buttons. A per-photo rest message would break the single-response invariant (ADR-0002). The intent callback is gated instead.
2. Button callbacks and photo batches → deterministic routing (0 LLM).
3. Free text only → `TEXT_FAST_ROUTE` (DeepSeek) for intent / topic-shift detection.
4. Voice → one `MULTIMODAL_AUDIO` transcription (Gemini), then the transcript takes the free-text path above: 850-char cap, classification, Layer-2 stickiness, committed turn. So May can add a price or a note by voice inside a follow-up. A silent or noise-only clip gets a fixed "please say it again" reply with no DeepSeek call and no session change.
5. The `general` route answers with a fixed help menu (0 LLM after classification).

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
| `awaiting_intent` | intent callback → workflow completes (knowledge saved, response built) | `awaiting_intent`, intent added to `completed_intents` (T5.1: another intent may still run within the TTL) |
| `awaiting_intent` | intent callback → workflow fails (LLM error, malformed reply, budget) | `awaiting_intent` (May may tap again within the TTL) |
| `awaiting_intent` | callback for an intent in `completed_intents` | unchanged (idempotent no-op, "這項已經處理好囉", also after the TTL) |
| `done` | any callback (all-blurry batch, or finished before T5.1) | `done` (idempotent no-op, gentle "已經處理好囉") |

- A new burst arriving while another batch is `awaiting_intent` opens a new batch; callbacks carry `batch_id`, so old buttons stay valid.
- **Startup orphan recovery (idempotent)**: batches left in `collecting` / `filtered` by a restart re-run the image gate over their persisted assets and proceed per the table, sending that batch's single response once.
- **`awaiting_intent` TTL (G4)**: 15 minutes (same as the session idle window), checked lazily against `media_batches.intent_expires_at` when a callback arrives. `callback_data` binds `batch_id`. Callbacks on a `done` or expired batch are idempotently rejected with a gentle resend prompt.

## 5. Core Database Schema (SQLite WAL + FTS5 -> Postgres Portable)
- Every table MUST contain `tenant_id TEXT NOT NULL` and `created_at`.
- **All timestamp columns use the custom `UTCDateTime` TypeDecorator** (UTC-aware on write and read, on SQLite and Postgres alike — ADR-0005).
- Every repository query requires explicit `tenant_id` and `user_id`. There are three documented exceptions. Identity resolution `(channel, external_user_id) -> (tenant_id, user_id)` is how both IDs are obtained. The startup orphan scan (`list_orphan_batches`, `status IN ('collecting','filtered')` across all tenants) is startup maintenance only and never serves a user request. Tenant-level FinOps aggregates (the Taipei-day ledger cost sum, the tenant's daily budget, and the `budget_alert_80_date` claim) are scoped by `tenant_id` only, because the budget belongs to the tenant, not to one person. They never return person-level rows.

1. `tenants`: `id`, `name`, `plan_tier`, `daily_budget_usd` (default `0.20`), `budget_alert_80_date` (Asia/Taipei date of the last 80% alert; persisted so restarts never resend), `config_json`
2. `users` (a person): `id`, `tenant_id`, `display_name`, `preferences_json`
3. `channel_identities`: `id`, `tenant_id`, `user_id`, `channel` (`telegram|line`), `external_user_id`, `UNIQUE(channel, external_user_id)` (ADR-0001)
4. `topic_sessions`: `id`, `tenant_id`, `user_id`, `topic_type` (`lecture|sales|memory|ios_help|general`), `status` (`active|closed`), `turn_count`, `extraction_count`, `state_json`, `recent_turns_json`, `last_active_at` + partial unique index (§4.5)
5. `media_batches`: `id`, `tenant_id`, `user_id`, `channel`, `media_group_id`, `status` (`collecting|filtered|awaiting_intent|done`), `raw_count`, `kept_count`, `reply_address` (persistent chat address for orphan recovery; never an ephemeral token), `intent_expires_at`
6. `media_assets`: `id`, `batch_id` (nullable: the adapter saves the file before any batch exists, and voice never joins one), `tenant_id`, `user_id`, `external_file_id` (cache only), `media_type`, `local_path` (POSIX string relative to `DATA_DIR`), `phash`, `blur_score`, `is_kept`, `is_blurry`
   - **Invariant**: every asset keeps its original file locally. `external_file_id` is never the only copy (Phase 2 LINE needs our own bytes).
7. `knowledge_items`: `id`, `tenant_id`, `user_id`, `category` (`jewelry|product|lecture|item_location|crm|ios_guide`), `title`, `content_text`, `structured_json`, `primary_asset_id`, `search_tokens` (+ FTS5 virtual table over `search_tokens`, ADR-0004)
   - Memory queries search only `item_location`. A location added in a follow-up updates the item in place (`StoragePort.update_knowledge`), so an old location is never found. Sales rewrites still save new rows.
8. `llm_usage_ledger`: `id`, `tenant_id`, `user_id`, `provider`, `model`, `prompt_tokens`, `completion_tokens`, `estimated_cost_usd`, `workflow_name`

## 6. FinOps Guardian & Dual-Track Taiwan Direct-Sales Compliance
- **FinOps**: Before EVERY LLM call, sum the tenant's ledger for the current Asia/Taipei day. The day boundary is computed in Python with `ZoneInfo("Asia/Taipei")` and converted to a UTC range for the query — never from process local time (ADR-0005). `>= 80%`: warn Discord Ops once per day (guarded by `tenants.budget_alert_80_date`, claimed by a conditional UPDATE before sending, so restarts and races never resend). `>= 100%`: block LLM, return friendly rest message, alert Discord Ops (deduplicated in memory per tenant and Taipei day; a restart may resend it once). The check runs before the call, so concurrent users of one tenant can overshoot by at most one call each (single-process, family scale).
- **Test isolation**: `tests/conftest.py` autouse fixture forces `APP_ENV=test` + `TEST_NO_SEND=1` and blocks unmocked outbound network at the httpx and httpx2 transports (google-genai and openai ship on httpx2), `socket.connect`, and `asyncio` `create_connection` (the Windows proactor loop bypasses `socket.connect`), so the guard holds even when `pytest` runs without `scripts/test.py`. The guard raises `NetworkBlockedError` (a `RuntimeError` subclass in `src/adapters/errors.py`). `DiscordWebhookNotifier` re-raises only that error and logs and swallows every other send failure, such as the closed-client error at shutdown.
- **Compliance (`src/domain/compliance.py`)**: Deterministic scanner using `src/domain/tw_prohibited_terms.yaml`. The dictionary ships with the code inside the image; `data/` is the runtime volume and is git-ignored. Always generate Version A (safe persuasive experience framing) + optional Version B (polished original intent with explicit legal risk `warning_note`). Version A always passes scan → `sanitize()` → 0 hits after the LLM, on the initial draft and on every follow-up rewrite. `sanitize()` replaces longest terms first, runs at most 3 rounds, then deletes survivors, so it never raises. Version B is kept only when it is non-empty and contains a prohibited term; its `warning_note` names the terms and laws. When May's own text (a text draft or a follow-up) carries a prohibited claim but the model dropped or softened Version B, her risky sentences (minus any leading instruction ending in a colon) are appended to the model's B, or to A when B is null, so her original intent is never lost (T5.1 live finding). Photo-only drafts have no words of hers and are unaffected.
- **Taiwan tone**: every Gemini prompt requires Traditional Chinese in everyday Taiwan wording and forbids Mainland terms (視頻 → 影片, 軟件 → 軟體, 親 → 您).

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
- **Dependencies**: `opencv-python-headless` (no libGL in slim images), `imagehash` (pHash dedup), `pillow-heif` (iPhone HEIC), `tzdata` (IANA zones on Windows and slim images).
- **Time & paths** (ADR-0005): UTC-aware storage via `UTCDateTime`; injected `Clock`; DB stores media paths as POSIX strings relative to `DATA_DIR`.
- **Container verification**: `docker compose run --rm app python scripts/test.py` is part of T1 and T5 DoD. It uses a container-local / named volume, never the Windows bind mount (SQLite WAL is unreliable over Docker Desktop file sharing).
- **Volumes**: `./data:/app/data` bind mount is for Linux prod only. On Windows, run the app natively with `uv run`; never open one SQLite file from a native process and a container at the same time.
- **Container runtime**: exec-form `CMD`, `init: true`, non-root UID owning `/app/data`, `restart: unless-stopped`, json-file log rotation (`max-size` / `max-file`).
- **Shutdown**: graceful shutdown is delegated to PTB `run_polling()`; no custom `loop.add_signal_handler` (unsupported on Windows).
- **Ops**: scheduled `sqlite3 .backup` of the DB; `.env` must be UTF-8 without BOM. Runbook: `docs/DEPLOY_LINUX.md`.
- **Startup order** (`src/main.py`): `alembic upgrade head` → idempotent seed (fixed tenant `default`; one `users` row per allowlisted Telegram ID; existing identities untouched) → `BatchAggregator.recover_orphans()` → PTB `run_polling()`. On stop, pending photo batches are drained while the bot can still send. An empty allowlist refuses to start. The `httpx` logger is held at WARNING, because PTB request logs carry the bot token in the URL. A log filter redacts the token from every record, and a token Telegram rejects stops the process with a message that does not contain it.
