# ADR-0003: Session Layer Transitions, Prompt Budget, and Single Extraction

- **Status**: Accepted (Architect ruling v1.3, 2026-11-21); G1–G4 resolved by ruling v1.3.1 (2026-11-21)
- **Source findings**: Review 2.1–2.8, 2.11

## Context
v1.2 had five problems in the session and budget rules:

- **Layer 1 vs Layer 2 contradiction.** Layer 1 closed its session on completion, but Layer 2 exists precisely to tweak "剛剛的文案".
- **Under-estimating token estimator.** The "conservative" estimator `char_len / 2` under-estimates CJK tokens.
- **Unachievable budget claim.** The promised 500–2,500 token calls were arithmetically impossible: the 3,000-token history cap alone exceeded it, and images and audio were unbudgeted.
- **Disguised recursive compaction.** Repeated JSON re-extraction was recursive compaction in disguise.
- **Missing operational rules.** Routing cost, concurrency, and batch lifecycle were undefined.

## Decision
1. **Layer 1 → Layer 2 transition**: after a Layer-1 workflow saves to `knowledge_items`, open (or reset) a clean Layer-2 session with `recent_turns = []` and `state_json = {"last_artifact_id": "...", "workflow": "..."}` — a reference, never artifact text. It allows follow-up within 15 minutes and closes lazily afterwards.
2. **Estimator**: `ceil(len(text) * 1.2)`, calibrated with API-returned `usage` stored in `llm_usage_ledger`.
3. **Prompt budget table**:

   | Call kind | Total | Slots |
   | :--- | :--- | :--- |
   | Text / follow-up | ≤ 4,500 | system ≤ 500, state_json ≤ 250, retrieved_context ≤ 1,200, recent_turns ≤ 1,500, current_user_msg ≤ 1,050 |
   | Multimodal | ≤ 7,500 (was 6,000; see Amendment) | ≤ 6 images; voice ≤ 120 s (rejected gently before LLM); free text ≤ 850 chars |

   `retrieved_context` is shared. A follow-up puts the truncated `last_artifact_id` text there; a memory query puts the Layer-3 top-3 there (G1). Free text over 850 chars is deterministically truncated with a gentle notice, since 850 × 1.2 = 1,020 ≤ 1,050 (G3).

4. **No recursive compaction**:
   - After every commit, `len(recent_turns) <= 6` AND history ≤ 1,500 tokens.
   - On the first ceiling hit, run one slot extraction via `TEXT_FAST_ROUTE`, validated by a fixed Pydantic schema and deterministically truncated to ≤ 250 tokens. It is never sent back to an LLM for rewriting. Old turns are dropped.
   - `extraction_count <= 1`: at most one *successful* extraction per session. A call that returns no response (both providers unavailable) is not an extraction; it stores nothing, bills nothing, and the next message at the ceiling may try again. A second ceiling hit after a successful extraction force-closes the session and invites May to start a new topic.
5. **Routing priority**:
   1. Budget at 100% → fixed rest message (0 LLM).
   2. Button callbacks and photo batches → deterministic routing (0 LLM).
   3. Free text → DeepSeek.
6. **Concurrency**:
   - Single-process assumption.
   - Partial unique index `topic_sessions(tenant_id, user_id) WHERE status='active'`.
   - Per-user in-process `asyncio.Lock`.
7. **`media_batches`**: explicit state table and idempotent startup orphan recovery (ARCHITECTURE §4.6).
8. **FinOps details**:
   - Per-model price table co-located with model IDs in config; an unknown model fails fast at startup.
   - The 80% alert date is persisted per tenant (`tenants.budget_alert_80_date`), so restarts do not resend it.
9. **Per-turn truncation (G2)**: every message written to `recent_turns` is deterministically truncated to ≤ 250 tokens. Full outputs stay in `knowledge_items` and enter prompts only via `last_artifact_id` → `retrieved_context`.
10. **`awaiting_intent` TTL (G4)**: 15 minutes, checked lazily via `media_batches.intent_expires_at`. `callback_data` binds `batch_id`. Callbacks on `done` or expired batches are idempotently rejected with a gentle resend prompt.

## Invariants
1. After every commit, `len(recent_turns) <= 6` and history ≤ 1,500 estimated tokens.
2. `extraction_count <= 1` per session (at most one successful extraction).
3. `len(state_json) <= 250` estimated tokens, schema-valid.
4. At most one `active` session per `(tenant_id, user_id)`.
5. Every LLM call's assembled prompt fits its budget row (≤ 4,500 text, ≤ 7,500 multimodal) before dispatch.
6. Callback and photo-batch routing make zero LLM calls.
7. Every message in `recent_turns` is ≤ 250 estimated tokens.
8. Free text entering a prompt is ≤ 850 chars.

## Consequences
- Memory of a conversation beyond one extraction is intentionally lost. Long-term facts belong in `knowledge_items` (Layer 3).
- Some extra DeepSeek calls on free text (topic-shift detection). Bounded by budget, and skipped at 100%.
- Single-process is now a stated constraint. Scaling out needs a distributed lock (Phase 3 concern).

## Resolved Questions (ruling v1.3.1)
- **G1** → shared `retrieved_context` ≤ 1,200 slot (Decision 3).
- **G2** → ≤ 250-token per-turn cap (Decision 9).
- **G3** → 850-char free-text cap and `current_user_msg` ≤ 1,050 (Decision 3).
- **G4** → 15-minute lazy TTL (Decision 10).

Original statements, kept for traceability:
- **G1 — artifact slot for follow-ups**: Layer 2 stores only `last_artifact_id`, but tweaking "剛剛的文案" requires the artifact text in the prompt. The text budget row has no slot for it (`state` is 250). Version A + B at ≤ 1,000 chars each is about 2,400 tokens by the 1.2 estimator.
- **G2 — single oversized assistant turn**: one assistant reply (e.g. a sales-copy result) can exceed the 1,500-token history ceiling by itself. Appending it would violate Invariant 1 on the first turn, and extraction would fire immediately.
- **G3 — message limit vs slot**: a single text message may be up to 2,000 chars (≈ 2,400 tokens), but `current_msg` is budgeted at 650 tokens.
- **G4 — `awaiting_intent` TTL**: how long intent buttons on a photo batch stay valid is not ruled.

T2 implements G4. T3 and T4 implement G1–G3.

## Amendment (T3 rulings D1–D6, 2026-11-21)
- **D5 multimodal budget**: Gemini 3 bills 1,120 tokens per image at its default resolution, so 6 images (6,720) already exceeded 6,000. The adapter fixes `MEDIA_RESOLUTION_MEDIUM` (560 per image), audio is estimated at a conservative 32 tokens per second, and the multimodal total rises to **7,500**. Worst Phase-1 case: 3 images + 120 s voice + system 500 + user message 1,050 = 7,070, so T4 needs no extra cap.
- **D1 photos at 100% budget**: photo events skip the budget gate (they make 0 LLM calls), because a rest message per photo would break ADR-0002. The intent callback is gated instead.
- **D2 tenant-level aggregate**: the FinOps ledger sum, daily budget, and 80% alert claim are scoped by `tenant_id` only. This is the third documented exception to the `tenant_id` + `user_id` query rule (ARCHITECTURE §5).
- **D3 100% alert**: deduplicated in memory per tenant and Taipei day. No migration; a restart may resend it once. The 80% alert stays persisted.
- **D4 voice length**: `IncomingEvent.audio_duration_s` (required for audio, > 0) comes from platform metadata, so Decision 3's 120 s rejection happens before any LLM call.
- **D6 second ceiling hit**: the session closes and the message gets the fixed "start a new topic" reply. No handler or extraction call runs for it. The message still pays the one `TEXT_FAST_ROUTE` classification that every free-text message pays, because classification runs before the ceiling check: a message that is really a topic shift opens a fresh session and is answered instead of being force-closed. The next message starts a clean session.
- **G failed extraction**: an extraction call that raises (both providers unavailable) is not counted; the next message at the ceiling may try again. `extraction_count` counts successful extractions only.

## Verification
- T3 DoD (c), (d), (g), (h), (i), (j): extraction-once, idle/topic reset with a frozen `Clock`, 0-LLM callbacks, concurrent single active session, fail-fast prices, voice rejection.
- T3 DoD (f): 80% alert once per Taipei day across a simulated restart.
- T4: Layer-1 completion opens exactly one Layer-2 session with only `last_artifact_id` + `workflow`.
- T2: `media_batches` orphan recovery idempotent.
