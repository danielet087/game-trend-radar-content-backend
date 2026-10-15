"""Refresh due Steam descriptions only, independently of eligibility and metadata.

python content_backend/localize_descriptions.py --data-dir /path/to/frontend/data
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import requests

from radar_backend.adapters.content_helpers import STORE_BROWSE, SteamRateLimit, request_json, upsert_sharded
from radar_backend.adapters.localized_descriptions import description_fields, merge_description_fields
from radar_backend.domain.description_refresh import RETRY_DELAY, due_at, next_check, retry_pending, select_due, timestamp
from radar_backend.domain.localized_descriptions import DESCRIPTION_CONTENT_KEYS
from radar_backend.publication import catalog
from radar_backend.publication.content_snapshot import strict_catalog
from radar_backend.state.json_documents import load_json, write_json

STATUS_FILE = "description_refresh_status.json"
LANGUAGES = ("english", "tchinese", "schinese")
BATCH_SIZE = 25


def fetch_locales(appids: list[int], cache_dir: Path | None = None) -> dict:
    """Always request fresh three-language evidence; cache is diagnostic output only."""
    result = {}
    with requests.Session() as session:
        session.headers["User-Agent"] = "GameTrendRadar/description-localization"
        for language in LANGUAGES:
            rows = {}
            for start in range(0, len(appids), BATCH_SIZE):
                payload = {"ids": [{"appid": appid} for appid in appids[start:start + BATCH_SIZE]],
                           "context": {"country_code": "TW", "language": language, "steam_realm": 1},
                           "data_request": {"include_basic_info": True}}
                data = request_json(session, STORE_BROWSE,
                                    params={"input_json": json.dumps(payload, separators=(",", ":"))}, attempts=2)
                items = (data.get("response") or {}).get("store_items")
                if not isinstance(items, list):
                    raise RuntimeError(f"Malformed Steam {language} description response")
                rows.update({str(row["appid"]): row for row in items
                             if isinstance(row, dict) and type(row.get("appid")) is int
                             and row.get("success", 1) == 1 and row.get("visible") is not False})
                time.sleep(1)
            if cache_dir:
                write_json(cache_dir / f"{language}.json", rows)
            result[language] = rows
    return result


def localize(data_dir: Path, cache_dir: Path | None = None, *, max_games: int = 100,
             max_seconds: int = 540, force: bool = False,
             now: datetime | None = None) -> dict:
    strict_catalog(data_dir, require_index=True)
    records = [load_json(path) for path in sorted((data_dir / "games").glob("*.json"))]
    now = now or datetime.now(timezone.utc)
    status_path = data_dir / STATUS_FILE
    previous_status = load_json(status_path) if status_path.is_file() else {}
    existing_appids = {str(row["appid"]) for row in records}
    checkpoints = {key: {name: item for name, item in value.items() if name not in {"translation_pending", "translation_retry_at"}}
                   for key, value in (previous_status.get("checkpoints") or {}).items()
                   if key in existing_appids}
    due = select_due(records, checkpoints, now, len(records))
    selected = records[:max_games] if force else due[:max_games]
    counts, updates = Counter(), []
    start = time.monotonic()
    attempted, failed, stop_reason = 0, [], ""

    def checkpoint_error(batch, error):
        nonlocal attempted
        # Failure cannot alter existing text or its successful checked-at time.
        for row in batch:
            appid = str(row["appid"])
            checkpoints[appid] = {"attempted_at": timestamp(now), "outcome": "error",
                                  "retry_at": timestamp(now + RETRY_DELAY), "error": str(error)[:400]}
            attempted += 1
            failed.append(int(appid))

    for offset in range(0, len(selected), BATCH_SIZE):
        if time.monotonic() - start >= max_seconds:
            stop_reason = "time_budget"
            break
        batch = selected[offset:offset + BATCH_SIZE]
        try:
            locales = fetch_locales([int(row["appid"]) for row in batch], cache_dir)
        except (RuntimeError, requests.RequestException, ValueError, TypeError) as error:
            checkpoint_error(batch, error)
            if isinstance(error, SteamRateLimit):
                stop_reason = "steam_rate_limit"
                break
            continue
        for previous in batch:
            if time.monotonic() - start >= max_seconds:
                stop_reason = "time_budget"
                break
            appid = int(previous["appid"])
            if any(str(appid) not in locales.get(language, {}) for language in LANGUAGES):
                checkpoint_error([previous], "Incomplete three-language Steam response")
                continue
            if any(not isinstance(locales[language][str(appid)].get("basic_info") or {}, dict)
                   for language in LANGUAGES):
                checkpoint_error([previous], "Malformed Steam description basic_info")
                continue
            texts = [(locales[language][str(appid)].get("basic_info") or {}).get("short_description")
                     for language in LANGUAGES]
            fields = description_fields(appid, *texts)
            record = {**{key: value for key, value in previous.items() if key not in DESCRIPTION_CONTENT_KEYS},
                      **fields, "description_checked_at": timestamp(now)}
            record["description_next_check_at"] = timestamp(next_check(merge_description_fields(previous, record), now))
            upsert_sharded(data_dir, record, previous["release_start"], force=True, now=now)
            record = load_json(data_dir / "games" / f"{appid}.json")
            checkpoints[str(appid)] = {"attempted_at": timestamp(now), "outcome": "success",
                                       "next_check_at": record["description_next_check_at"]}
            counts[record["short_description_source"]] += 1
            attempted += 1
            updates.append(appid)
        write_json(status_path, {"schema_version": 1, "generated_at": timestamp(now), "checkpoints": checkpoints})
        if stop_reason == "time_budget":
            break

    current = {int(row["appid"]): row for row in records}
    for appid in updates:
        current[appid] = load_json(data_dir / "games" / f"{appid}.json")
    remaining = select_due(list(current.values()), checkpoints, now, len(current))
    retry_appids = retry_pending(list(current.values()), checkpoints)
    pending_appids = {int(row["appid"]) for row in remaining} | set(retry_appids)
    deadlines = [due_at(row, checkpoints.get(str(row["appid"]), {}), now) for row in current.values()]
    report = {"schema_version": 1, "generated_at": timestamp(now), "checkpoints": checkpoints,
              "due_count": len(due), "attempted": attempted, "refreshed": len(updates),
              "updated_appids": updates, "failed_appids": failed, "sources": dict(counts),
              "pending_count": len(pending_appids), "retry_pending_appids": retry_appids,
              "complete": not pending_appids,
              "stop_reason": stop_reason or ("game_budget" if len(due) > len(selected) else ""),
              "next_due_at": timestamp(min(deadlines)) if deadlines else None}
    write_json(status_path, report)
    # Safely migrate derived browser projections when FIELDS gained description
    # data, including runs with no due games. Strict source validation ran first.
    catalog._rebuild_small_indexes(data_dir, generated_at=timestamp(now), now=now)
    print(json.dumps({key: value for key, value in report.items() if key != "checkpoints"}, ensure_ascii=False), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, help="Write diagnostic responses; never reuse old locale evidence")
    parser.add_argument("--max-games", type=int, default=100)
    parser.add_argument("--max-seconds", type=int, default=540)
    parser.add_argument("--force", action="store_true", help="Refresh up to max-games even before their next deadline")
    args = parser.parse_args()
    if not (args.data_dir / "games").is_dir():
        parser.error("data-dir must contain the existing published games directory")
    if args.max_games < 1 or args.max_seconds < 1:
        parser.error("budgets must be positive")
    localize(args.data_dir, args.cache_dir, max_games=args.max_games, max_seconds=args.max_seconds,
             force=args.force)


if __name__ == "__main__":
    main()
