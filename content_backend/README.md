# Game Trend Radar Content Backend

Event-driven Steam metadata enrichment backend.

## Responsibility

This backend does **not** discover games and does **not** verify Followers.
It only accepts a game after Backend A has verified Steam Community
`memberCount >= 5000`.

For each accepted AppID it refreshes official/public Steam content:

- Taiwan release timestamp/date
- English / Traditional Chinese / Simplified Chinese Store names
- game language support
- official player categories (single-player, co-op, PvP, MMO and local multiplayer)
- official header / main capsule artwork
- genres
- public Steam Store tags when available
- short description
- official Followers value supplied by Backend A

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

`repository_dispatch` event type: `steam_game_qualified`

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

## 分層架構

內容補充與對帳的 domain、application、HTTP adapter、JSON state、publication 及 job 已放入 `radar_backend/`。舊 CLI 與函數入口維持相容。責任分配、執行方式及保留債務見 [後端架構說明](../docs/backend-architecture.md)。

## Independent description refresh

`localize_descriptions.py` refreshes only due descriptions, with fresh English,
Traditional Chinese and Simplified Chinese Store Browse requests in batches of
25. Each run is bounded to 100 games / 540 seconds by default. Official Chinese
always replaces the English fallback, even when English has not changed. Existing
Chinese and its successful check timestamp survive transport failures; retry
checkpoints are persisted for six hours and a 429 stops the current run.

The description workflow is dispatched every six hours by the existing
Cloudflare scheduler and also runs once after relevant main-branch changes.
It does not fetch Followers, eligibility, artwork, tags or release dates. The
frozen publication adapter updates only description fields on the latest
frontend and publishes all catalog
projections plus retry state in one acknowledged commit. See the repository
README for source precedence and refresh intervals. English is a valid result
when Chinese is unavailable; no automatic machine translation runs.
