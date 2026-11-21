# ADR-0001: Split Person Identity from Channel Identity

- **Status**: Accepted (Architect ruling v1.3, 2026-11-21)
- **Source finding**: Review 3.1 (`docs/reviews/2026-11-21-architect-blind-spots.md`)

## Context
The v1.2 schema stored `channel` and `external_user_id` on the `users` row, so one user could belong to exactly one channel. When May moves from Telegram to LINE in Phase 2, her LINE `userId` would create a new user, and every `knowledge_items` row (her memory stash, lecture digests, product copy) would be orphaned under the old Telegram user. Separately, nothing defined how an incoming Telegram user resolves to a `tenant_id`.

## Decision
- `users` represents a **person**: `id`, `tenant_id`, `display_name`, `preferences_json`.
- New table `channel_identities`: `id`, `tenant_id`, `user_id`, `channel` (`telegram|line`), `external_user_id`, with `UNIQUE(channel, external_user_id)`.
- Adapters resolve `(channel, external_user_id) -> (tenant_id, user_id)` and put only internal IDs into `IncomingEvent`. Workflows and repositories never see channel user IDs.
- Ships in T1's initial migration, not deferred to Phase 2.

## Invariants
1. A `(channel, external_user_id)` pair maps to at most one person.
2. All domain data (`knowledge_items`, `topic_sessions`, `media_batches`, ledger) keys on internal `user_id`, never on a channel ID.
3. Identity resolution is the **only** repository query not given `tenant_id` + `user_id` up front, because it is how they are obtained.

## Consequences
- Phase 2 linking = insert one `channel_identities` row for May's LINE `userId` pointing at her existing `user_id`. Zero memory loss, zero workflow changes.
- Phase 1 allowlisted Telegram users must be seeded into `users` + `channel_identities`; the seeding mechanism is a T1 implementation detail.
- One extra join on every inbound event (negligible at this scale).

## Verification
- T1: uniqueness test on `(channel, external_user_id)`.
- T1: one person with a Telegram and a (fixture) LINE identity reads the same `knowledge_items`.
- T5: allowlisted Telegram user resolves to internal IDs; unknown user is rejected.
