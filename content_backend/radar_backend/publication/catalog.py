"""Publish accepted records and their coherent AppID/month/browser projections."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
from localized_descriptions import merge_description_fields
from steam_taxonomy import preserve_taxonomy
from steam_player_modes import has_verified_categories
from radar_core.domain.twitch_admission import is_twitch_qualified
from radar_backend.domain.content import TAIPEI, valid_date
from radar_backend.domain.release import keep_newer_release
from radar_backend.state.json_documents import load_json, write_json as _write_json
from public_catalog import write_catalog_projection


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def _update_month_file(data_dir: Path, month: str, appid: int, record: dict | None) -> None:
    path = data_dir / "calendar" / f"{month}.json"
    doc = {"version": 2, "month": month, "games": []}
    if path.exists():
        try:
            loaded = load_json(path)
            if isinstance(loaded, dict):
                doc.update(loaded)
        except (OSError, ValueError, TypeError):
            pass
    games = [
        row for row in (doc.get("games") or [])
        if isinstance(row, dict) and int(row.get("appid", -1)) != appid
    ]
    if record is not None:
        games.append(record)
    games.sort(key=lambda g: (
        str(g.get("release_start") or "9999-12-31"),
        -int(g.get("followers") or 0),
        int(g.get("appid", 0)),
    ))
    if games:
        doc.update({
            "version": 2,
            "generated_at": utc_now(),
            "month": month,
            "count": len(games),
            "games": games,
        })
        _write_json(path, doc)
    elif path.exists():
        path.unlink()


def _rebuild_small_indexes(data_dir: Path) -> None:
    rows = []
    games_dir = data_dir / "games"
    for path in games_dir.glob("*.json"):
        try:
            row = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        try:
            appid = int(row.get("appid"))
            followers = int(row.get("followers"))
        except (TypeError, ValueError):
            continue
        release = row.get("release_start")
        if appid <= 0 or (followers < 3000 and not is_twitch_qualified(row)) or not valid_date(release):
            continue
        rows.append(row)

    rows.sort(key=lambda g: (
        str(g.get("release_start") or "9999-12-31"),
        -int(g.get("followers") or 0),
        int(g.get("appid", 0)),
    ))
    months = sorted({str(row["release_start"])[:7] for row in rows})
    now = utc_now()
    today = datetime.now(TAIPEI).date()
    today_s = today.isoformat()
    released_from = (today - timedelta(days=30)).isoformat()

    upcoming = [
        int(row["appid"]) for row in rows
        if row["release_start"] >= today_s and (int(row["followers"]) >= 5000 or is_twitch_qualified(row))
    ]
    released = [
        int(row["appid"]) for row in rows
        if released_from <= row["release_start"] < today_s
        and (
            int(row["followers"]) >= 5000
            or is_twitch_qualified(row)
            or (
                int(row["followers"]) > 3000
                and row.get("recent_source") in {"tracked_release", "direct_release"}
            )
        )
    ]

    prior_index = {}
    try:
        prior_index = load_json(data_dir / "index.json")
    except (OSError, ValueError, TypeError):
        prior_index = {}
    projection = write_catalog_projection(data_dir, rows, now)
    _write_json(data_dir / "index.json", {
        "version": 2,
        "generated_at": now,
        "source": "Steam AppID-sharded public catalog",
        "game_count": len(rows),
        "months": months,
        "calendar_path": "calendar/{YYYY-MM}.json",
        "game_path": "games/{appid}.json",
        "lists": {
            "upcoming": "lists/upcoming.json",
            "released": "lists/released.json",
        },
        "legacy_fallback": "steam_upcoming.json",
        "release_date_audited": prior_index.get("release_date_audited") is True,
        **projection,
    })
    _write_json(data_dir / "lists" / "upcoming.json", {
        "version": 2, "generated_at": now,
        "count": len(upcoming), "appids": upcoming,
    })
    _write_json(data_dir / "lists" / "released.json", {
        "version": 2, "generated_at": now,
        "count": len(released), "appids": released,
    })
    # Legacy readers must see exactly the same catalog as the sharded index.
    # Preserve released titles; do not derive this file from only upcoming AppIDs.
    _write_json(data_dir / "steam_upcoming.json", {
        "version": 2, "generated_at": now,
        "count": len(rows), "games": rows,
    })


def _shard_in_sync(data_dir: Path, record: dict) -> bool:
    """Existing AppID is not published until its month, indexes and fallback agree."""
    appid = int(record["appid"])
    release = str(record["release_start"])
    try:
        month = load_json(data_dir / "calendar" / f"{release[:7]}.json")
        index = load_json(data_dir / "index.json")
        legacy = load_json(data_dir / "steam_upcoming.json")
        upcoming = load_json(data_dir / "lists" / "upcoming.json")
        released = load_json(data_dir / "lists" / "released.json")
        row = next(x for x in month["games"] if int(x["appid"]) == appid)
        fallback = next(x for x in legacy["games"] if int(x["appid"]) == appid)
        today = datetime.now(TAIPEI).date()
        day = datetime.fromisoformat(release).date()
        expected_upcoming = day >= today and (int(record["followers"]) >= 5000 or is_twitch_qualified(record))
        expected_released = (
            today - timedelta(days=30) <= day < today
            and (int(record["followers"]) >= 5000 or is_twitch_qualified(record) or (
                int(record["followers"]) > 3000
                and record.get("recent_source") in {"tracked_release", "direct_release"}
            ))
        )
        return (
            row == record
            and fallback == record
            and release[:7] in index["months"]
            and int(index["game_count"]) == int(legacy["count"]) == len(legacy["games"])
            and int(month["count"]) == len(month["games"])
            and (appid in upcoming["appids"]) == expected_upcoming
            and (appid in released["appids"]) == expected_released
        )
    except (OSError, ValueError, TypeError, KeyError, StopIteration):
        return False


def excluded_public_appids(data_dir: Path) -> set[int]:
    """Fail closed: never recreate games excluded by the official Steam adult audit."""
    path = data_dir / "excluded_appids.json"
    if not path.is_file():
        raise RuntimeError(f"Required adult exclusion list missing: {path}")
    payload = load_json(path)
    if not isinstance(payload.get("appids"), list):
        raise RuntimeError("Malformed adult exclusion list")
    return {int(appid) for appid in payload["appids"]}


def upsert_sharded(
    data_dir: Path,
    record: dict,
    event_release_date: str,
    *,
    force: bool = False,
) -> bool:
    """Update one AppID file and only the affected calendar shard."""
    appid = int(record["appid"])
    if appid in excluded_public_appids(data_dir):
        raise RuntimeError(f"AppID {appid} excluded by official Steam adult-content audit")
    path = data_dir / "games" / f"{appid}.json"
    existing = {}
    if path.exists():
        try:
            loaded = load_json(path)
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError, TypeError):
            existing = {}

    record = keep_newer_release(existing, record)
    if 'twitch_admission' in record and not is_twitch_qualified(record):
        raise RuntimeError(f'AppID {appid} has an invalid Twitch-source Steam qualification')
    # An older queued event must not roll back a more recent official result.
    try:
        old_at = datetime.fromisoformat(str(existing.get("follower_checked_at")).replace("Z", "+00:00"))
        new_at = datetime.fromisoformat(str(record.get("follower_checked_at")).replace("Z", "+00:00"))
        if new_at < old_at and any(record.get(key) != existing.get(key) for key in ("followers", "release_start")):
            raise RuntimeError(f"Stale official event for AppID {appid}; preserving newer public data")
    except (ValueError, TypeError):
        pass

    signature = f"{appid}:{record['followers']}:{record['release_start']}"
    already = (
        existing.get("content_enrichment_signature") == signature
        and existing.get("header_image")
        and isinstance(existing.get("language_support"), dict)
        and isinstance(existing.get("tags"), list)
        and (int(existing.get("followers", 0)) >= 5000 or is_twitch_qualified(existing))
        and existing.get('twitch_admission') == record.get('twitch_admission')
        and (not has_verified_categories(record) or (
            has_verified_categories(existing) and existing['categories'] == record['categories']))
    )
    if already and not force:
        if _shard_in_sync(data_dir, existing):
            return False
        # A previous push may have published only the AppID file, or an
        # optimistic retry may have reset tracked indexes but kept an
        # untracked AppID. Repair indexes without another Steam request.
        _update_month_file(data_dir, str(existing["release_start"])[:7], appid, existing)
        _rebuild_small_indexes(data_dir)
        return True

    merged = preserve_taxonomy(existing, merge_description_fields(existing, record))
    merged["content_enrichment_signature"] = signature
    merged["storage_version"] = 2
    _write_json(path, merged)

    new_release = str(merged["release_start"])
    new_month = new_release[:7]
    # A prior partial publication can leave the AppID in an older month even
    # after its individual record moved. Remove every stale occurrence.
    for month_path in (data_dir / "calendar").glob("????-??.json"):
        if month_path.stem == new_month:
            continue
        month_doc = load_json(month_path)
        if any(int(row.get("appid", -1)) == appid for row in month_doc.get("games", [])):
            _update_month_file(data_dir, month_path.stem, appid, None)
    _update_month_file(data_dir, new_month, appid, merged)
    _rebuild_small_indexes(data_dir)
    return True
