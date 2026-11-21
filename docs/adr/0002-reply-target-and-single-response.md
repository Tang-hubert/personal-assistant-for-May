# ADR-0002: `ReplyTarget` and the Single-Response Invariant

- **Status**: Accepted (Architect ruling v1.3, 2026-11-21)
- **Source findings**: Review 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9

## Context
v1.2 used `reply_handle: str` for both Telegram `chat_id` and LINE `replyToken`. They have different semantics:

- Telegram `chat_id` is persistent and reusable without limit.
- LINE `replyToken` is single-use and expires shortly after the event (exact window to be confirmed against LINE docs). Once it expires, only the Push API works, and Push consumes the OA's monthly message quota.

A Telegram habit of "ack with 處理中..., then send the result" would burn the LINE reply token on the ack. Other channel limits were also not encoded in the contract: Telegram `CopyTextButton` max 256 chars, LINE `ClipboardAction` max 1,000 chars, Telegram `callback_data` max 64 bytes, LINE quick-reply label max 20 chars, and LINE Flex requires `altText`.

## Decision
1. `IncomingEvent.reply_target: ReplyTarget` with
   - `address: str` — persistent (Telegram `chat_id` / LINE `userId`)
   - `ephemeral_token: str | None` — LINE `replyToken`; `None` on Telegram
   - `token_expires_at: datetime | None`

   Only adapters read it. The adapter chooses reply vs push; workflows never know.
2. **Single-response invariant**: one `IncomingEvent` (or one media batch) produces at most one `BotResponse`. Progress feedback is chat actions only (Telegram `typing` / `upload_photo`, LINE loading animation). Intermediate text messages are forbidden.
3. Contract limits enforced by Pydantic validators in `src/domain/models.py`:
   - `CopyableBlock.body_text` ≤ 1,000 chars. TelegramAdapter renders ≤ 256 chars as `CopyTextButton`, and 257–1,000 chars as an HTML-escaped `<pre>` block.
   - `ActionButton.label` ≤ 20 chars; `callback_data` is a short server-side token (e.g. `b:<batch_id>:lecture`, ≤ 64 bytes).
   - `BotResponse.alt_text` is derived from `title + bullets`, truncated to 400 chars.
4. Event-normalization additions:
   - `event_id` idempotency key (Telegram `update_id`, LINE `webhookEventId`).
   - `event_type = "unsupported"` with one fixed gentle reply.
   - Image documents and HEIC normalized to `"image"`.
   - Optional `group_index` / `group_total`.
5. Media originals are always persisted locally. `external_file_id` is a cache only, because LINE can only send images by our own HTTPS URL.

## Invariants
1. `count(BotResponse per event or batch) <= 1`.
2. No workflow or domain module imports or reads `ReplyTarget` internals.
3. Every `media_assets` row has a local original file.
4. A redelivered `event_id` causes no second side effect.

## Consequences
- Phase 2 `LineAdapter` implements reply-then-fallback-to-push inside the adapter only.
- Long workflows (multi-image Gemini) may exceed the LINE reply window and fall back to push, which costs quota. This is accepted, and the adapter owns it.
- Workflows cannot stream partial results. Acceptable for a senior-friendly, one-answer UX.

## Verification
- T1: validator tests for every limit above.
- T5: CopyTextButton/`<pre>` boundary at 255/256/257 chars; redelivered `update_id` processed once; `unsupported` reply; image document normalization; no handler sends more than one message per event.
- T2: a media batch emits exactly one `BotResponse`.
