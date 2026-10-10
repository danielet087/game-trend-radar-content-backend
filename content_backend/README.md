# Game Trend Radar Content Backend

Event-driven Steam metadata enrichment backend.

## Responsibility

This backend does **not** discover games and does **not** verify Followers.
It accepts official Steam Community `memberCount >= 5000`, or the existing
verified Twitch discovery with the same Steam game identity, exact Taiwan date
and adult-content checks. Missing GroupID can use that Twitch qualification;
it does not create a Steam Followers value.

For each accepted AppID it refreshes official/public Steam content:

- Taiwan release timestamp/date
- English / Traditional Chinese / Simplified Chinese Store names
- game language support
- official player categories (single-player, co-op, PvP, MMO and local multiplayer)
- official header / main capsule artwork
- genres
- public Steam Store tags when available
- short description
- official Followers value supplied by Backend A, or explicit unavailable state

The enriched record is then upserted into
`game-trend-radar/data/steam_upcoming.json`.

The operation is idempotent through
`content_enrichment_signature = appid:followers:release_date`.

Player modes come from the same AppID's official Steam `supported_player_categoryids`
or verified `appdetails.categories`, not community tags or unrelated features such
as Remote Play Together and Family Sharing. The public `categories` array keeps
`id` and `description`, plus source and check time. Missing data remains unknown;
a temporary failure preserves the previous verified categories. The existing
bounded content reconciliation queue backfills older records with one Browse
request when only player categories are missing, without querying Followers or
changing release dates. No additional schedule is created.

## Event contract

`repository_dispatch` event types: `steam_game_qualified`, `steam_game_refresh`,
`steam_game_twitch_discovered`.

Required payload:

```json
{
  "appid": 4115450,
  "official_followers": 5000,
  "release_date": "2026-10-29",
  "official_checked_at_taipei": "2026-09-23T18:00:00+08:00"
}
```

A future physical split into a dedicated repository only requires moving this
directory and its workflow, then changing Backend A's dispatch target.

## Missing GroupID with Twitch admission

The producer must supply the original validated `twitch_admission` proof and
these follower fields for an unavailable observation:

```json
{
  "official_followers": null,
  "official_checked_at_taipei": null,
  "follower_source": null,
  "group_id64": null,
  "official_ge5000": false,
  "follower_status": "unavailable_group_id",
  "follower_unavailable_at": "2026-10-10T03:00:00Z"
}
```

The unavailable timestamp must be timezone-aware and at least the admission
check time. Before collection, the workflow checks the proof against the exact
frontend commit's tracking and discovery snapshots. A rate limit, absent proof,
shared series identity, vague date or failed adult screen cannot use this path.
The CLI carries the value as `--followers null`, plus `--follower-status`,
`--follower-unavailable-at` and the existing `--twitch-admission` file.

Followers stay JSON `null` in the detail shard, calendar, browser catalog and
legacy fallback; a measured zero stays numeric `0`. Content does not query or
invent Followers. A later verified count replaces the unavailable state, and
a subsequent missing-group event preserves any existing genuine measurement.
Enrichment, reconciliation and frozen publication retain that count while still
publishing valid metadata changes. Unknown events cannot claim an ordinary
record without the complete Twitch identity/date/content proof.

## 分層架構

內容補充與對帳的 domain、application、HTTP adapter、JSON state、publication 及 job 已放入 `radar_backend/`。舊 CLI 與函數入口維持相容。責任分配、執行方式及保留債務見 [後端架構說明](../docs/backend-architecture.md)。
