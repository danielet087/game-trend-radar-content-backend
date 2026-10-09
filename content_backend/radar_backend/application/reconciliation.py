"""Bounded reconciliation use case with injectable source and publication ports."""

from __future__ import annotations
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from radar_core.domain.twitch_admission import normalize_twitch_admission
from radar_backend.domain.content import TAIPEI, valid_date
from radar_backend.domain.catalog import (
    metadata_gaps as _metadata_gaps,
    qualified_source,
    enrichment_signature,
)
from radar_backend.domain import player_categories
from radar_backend.domain.release import keep_newer_release as _keep_newer_release


@dataclass(frozen=True)
class ReconcilePorts:
    read_json: Callable
    write_json: Callable
    excluded: Callable
    build_record: Callable
    refresh_categories: Callable
    upsert: Callable
    in_sync: Callable
    rebuild: Callable
    session_factory: Callable
    monotonic: Callable
    now: Callable
    utc_now: Callable
    transport_error: type[Exception]
    rate_limit: type[Exception]
    preserve_release: Callable | None = None
    metadata_gaps: Callable | None = None
    verified_categories: Callable | None = None
    exists: Callable = Path.exists


def reconcile(
    master_path: Path,
    data_dir: Path,
    max_enrich: int,
    *,
    ports: ReconcilePorts,
    max_seconds: int = 2400,
    cache_dir: Path | None = None,
) -> dict:
    read_json = ports.read_json
    excluded_public_appids = ports.excluded
    build_record = ports.build_record
    refresh_player_categories = ports.refresh_categories
    upsert_sharded = ports.upsert
    _shard_in_sync = ports.in_sync
    _rebuild_small_indexes = ports.rebuild
    _write_json = ports.write_json
    utc_now = ports.utc_now

    def category_checked_at(record):
        return player_categories._category_checked_at(
            record,
            parse_time=datetime.fromisoformat,
            now=lambda: ports.now(timezone.utc),
        )

    def default_verified_categories(record):
        return player_categories.has_verified_categories(
            record,
            validate_categories=player_categories.valid_categories,
            category_checked_at=category_checked_at,
            sources=(
                player_categories.BROWSE_SOURCE,
                player_categories.APPDETAILS_SOURCE,
            ),
        )

    has_verified_categories = (
        ports.verified_categories
        if ports.verified_categories is not None
        else default_verified_categories
    )

    def preserve_categories(existing, incoming):
        return player_categories.preserve_player_categories(
            existing,
            incoming,
            verified_categories=has_verified_categories,
            category_checked_at=category_checked_at,
        )

    def default_preserve_release(existing, incoming):
        return _keep_newer_release(
            existing, incoming, preserve_categories=preserve_categories
        )

    keep_newer_release = (
        ports.preserve_release
        if ports.preserve_release is not None
        else default_preserve_release
    )

    def default_metadata_gaps(record, source):
        return _metadata_gaps(
            record,
            source,
            preserve_release=keep_newer_release,
            verified_categories=has_verified_categories,
        )

    metadata_gaps = (
        ports.metadata_gaps
        if ports.metadata_gaps is not None
        else default_metadata_gaps
    )
    started = ports.monotonic()
    master = read_json(master_path, {})
    blocked = excluded_public_appids(data_dir)
    today = ports.now(TAIPEI).date().isoformat()
    qualified = {}
    for source in master.get("games", []):
        if not isinstance(source, dict):
            continue
        try:
            appid, followers = int(source["appid"]), int(source["followers"])
        except (KeyError, TypeError, ValueError):
            continue
        day = source.get("release_start")
        historical = (
            valid_date(day)
            and day < today
            and ports.exists(data_dir / "games" / f"{appid}.json")
        )
        if qualified_source(
            source, appid, followers, day, historical=historical, blocked=blocked
        ):
            current = read_json(data_dir / "games" / f"{appid}.json", {})
            qualified[appid] = keep_newer_release(current, source)
    status_path = data_dir / "content_refresh_status.json"
    previous = read_json(status_path, {})
    failures = dict(previous.get("failures") or {})
    counts = {"attempted": 0, "enriched": 0, "repaired_indexes": 0}
    session = ports.session_factory()
    session.headers["User-Agent"] = "GameTrendRadarContentReconcile/2.0"
    # Future launches first, then recent historical records. A broken title
    # never starves all later AppIDs in the same run.
    ordered = sorted(
        qualified.items(),
        key=lambda x: (x[1]["release_start"] < today, x[1]["release_start"], x[0]),
    )
    for appid, source in ordered:
        path = data_dir / "games" / f"{appid}.json"
        record = read_json(path)
        gaps = metadata_gaps(record, source)
        if (
            gaps
            and counts["attempted"] < max_enrich
            and ports.monotonic() - started < max_seconds
        ):
            signature = enrichment_signature(appid, source)
            admission = normalize_twitch_admission(
                source.get("twitch_admission"), appid
            )
            prior_failure = failures.get(str(appid), {})
            retry_at = prior_failure.get("retry_after", "")
            if (
                prior_failure.get("signature") == signature
                and retry_at
                and retry_at > utc_now()
            ):
                continue
            counts["attempted"] += 1
            cached_path = cache_dir / f"{appid}.json" if cache_dir else None
            cached = read_json(cached_path) if cached_path else None
            try:
                if gaps == ["categories"]:
                    # Player-mode backfill uses the existing bounded content
                    # queue and preserves every accepted date/Followers field.
                    enriched = {**record, **refresh_player_categories(session, appid)}
                elif (
                    "release_date_diagnostics" not in gaps
                    and cached
                    and cached.get("signature") == signature
                    and cached.get("record", {}).get("content_enrichment_version", 0)
                    >= 5
                    and has_verified_categories(cached.get("record", {}))
                ):
                    enriched = cached["record"]
                else:
                    enriched = build_record(
                        session,
                        appid=appid,
                        followers=int(source["followers"]),
                        event_release_date=source["release_start"],
                        follower_checked_at=source.get("follower_checked_at"),
                        allow_historical=source["release_start"] < today
                        and isinstance(record, dict),
                        twitch_admission=admission,
                    )
                    if cached_path:
                        _write_json(
                            cached_path, {"signature": signature, "record": enriched}
                        )
                upsert_sharded(data_dir, enriched, source["release_start"], force=True)
                record = read_json(path)
                counts["enriched"] += 1
                failures.pop(str(appid), None)
                remaining = metadata_gaps(record, source)
                if remaining:
                    failures[str(appid)] = {
                        "signature": signature,
                        "reason": ",".join(remaining),
                        "attempted_at": utc_now(),
                        "retry_after": (ports.now(TAIPEI) + timedelta(hours=6))
                        .astimezone(timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                print(
                    "CONTENT_REPAIRED",
                    appid,
                    "remaining",
                    ",".join(remaining) or "none",
                    flush=True,
                )
            except (RuntimeError, ports.transport_error, ValueError, OSError) as exc:
                failures[str(appid)] = {
                    "signature": signature,
                    "reason": str(exc)[:220],
                    "attempted_at": utc_now(),
                    "retry_after": (ports.now(TAIPEI) + timedelta(hours=6))
                    .astimezone(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                print("CONTENT_DEFERRED", appid, type(exc).__name__, flush=True)
                if isinstance(exc, ports.rate_limit):
                    break
    # Repair all local records even after a rate limit or while remote fetches
    # are cooling down. No Steam calls are needed to repair derived files.
    for appid in qualified:
        record = read_json(data_dir / "games" / f"{appid}.json")
        if isinstance(record, dict) and not _shard_in_sync(data_dir, record):
            upsert_sharded(data_dir, record, record["release_start"], force=True)
            counts["repaired_indexes"] += 1
    # Materialize the small browser catalog even when no game needed enrichment.
    _rebuild_small_indexes(data_dir)
    pending = {}
    for appid, source in qualified.items():
        record = read_json(data_dir / "games" / f"{appid}.json")
        gaps = metadata_gaps(record, source)
        if gaps:
            pending[str(appid)] = gaps
        if record and not _shard_in_sync(data_dir, record):
            raise RuntimeError(f"Public catalog inconsistent for AppID {appid}")
    translation_pending = [
        appid
        for appid in qualified
        if (read_json(data_dir / "games" / f"{appid}.json", {}) or {}).get(
            "short_description_language"
        )
        != "zh-TW"
    ]
    result = {
        "description_translation_pending": translation_pending,
        "description_translation_pending_count": len(translation_pending),
        "generated_at": utc_now(),
        "qualified_master": len(qualified),
        **counts,
        "pending_count": len(pending),
        "pending_enrichment": pending,
        "failures": {key: value for key, value in failures.items() if key in pending},
        "complete": not pending,
        "public_count": read_json(data_dir / "index.json")["game_count"],
    }
    _write_json(status_path, result)
    print("STEAM_CATALOG_RECONCILE", json.dumps(result, ensure_ascii=False), flush=True)
    return result
