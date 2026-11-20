"""Refresh due Steam media files (hero art, screenshots, trailers, credits).

python content_backend/refresh_media.py --data-dir /path/to/frontend/data

Only data/media/, data/store_facts.json and the status file are written. A
game gets a media file within one run of entering the catalog, is re-checked
weekly, and loses its file when it leaves the catalog or is excluded. Price,
CSRR rating and Steam Deck status are checked for every game on every run.
The Steam versions of console games (data/steam_references.json) are treated
the same way, so their game pages are built on the Steam version too.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import requests

from radar_backend.adapters.content_helpers import STORE_BROWSE, SteamRateLimit, request_json
from radar_backend.domain.steam_media import (MEDIA_DIR, RETRY_DELAY, SCHEMA_VERSION, STATUS_FILE, due_at,
                                              media_content, media_document, retry_pending, same_content,
                                              timestamp, valid_document)
from radar_backend.domain.steam_store_facts import (FACTS_FILE, facts_document, needs_publication,
                                                    same_facts, store_facts)
from radar_backend.domain.steam_store_facts import valid_document as valid_facts_document
from radar_backend.domain.steam_references import REFERENCES_FILE
from radar_backend.domain.steam_references import valid_document as valid_references_document
from radar_backend.publication import catalog
from radar_backend.publication.content_snapshot import strict_catalog
from radar_backend.state.json_documents import load_json, write_json

BATCH_SIZE = 50
DATA_REQUEST = {"include_assets": True, "include_screenshots": True, "include_trailers": True,
                "include_basic_info": True}
FACTS_REQUEST = {"include_ratings": True, "include_platforms": True, "include_all_purchase_options": True}


def fetch_items(appids: list[int], data_request: dict = DATA_REQUEST) -> dict:
    """Fresh Taiwan Store Browse items keyed by AppID string."""
    rows = {}
    with requests.Session() as session:
        session.headers["User-Agent"] = "GameTrendRadar/steam-media"
        for start in range(0, len(appids), BATCH_SIZE):
            payload = {"ids": [{"appid": appid} for appid in appids[start:start + BATCH_SIZE]],
                       "context": {"country_code": "TW", "language": "tchinese", "steam_realm": 1},
                       "data_request": data_request}
            data = request_json(session, STORE_BROWSE,
                                params={"input_json": json.dumps(payload, separators=(",", ":"))}, attempts=2)
            items = (data.get("response") or {}).get("store_items")
            if not isinstance(items, list):
                raise RuntimeError("Malformed Steam media response")
            rows.update({str(row["appid"]): row for row in items
                         if isinstance(row, dict) and type(row.get("appid")) is int
                         and row.get("success", 1) == 1 and row.get("visible") is not False})
            time.sleep(1)
    return rows


def reference_appids(data_dir: Path) -> set[int]:
    """Steam versions of public console games; an unreadable file adds none."""
    path = data_dir / REFERENCES_FILE
    try:
        document = load_json(path) if path.is_file() else None
    except (OSError, ValueError, TypeError):
        return set()
    return {row["appid"] for row in document["games"]} if valid_references_document(document) else set()


def media_path(data_dir: Path, appid: int) -> Path:
    return data_dir / MEDIA_DIR / f"{appid}.json"


def load_document(data_dir: Path, appid: int) -> dict | None:
    """An unreadable or malformed file counts as missing and is rewritten."""
    path = media_path(data_dir, appid)
    if not path.is_file():
        return None
    try:
        document = load_json(path)
    except (OSError, ValueError, TypeError):
        return None
    return document if valid_document(document, appid) else None


def refresh_store_facts(data_dir: Path, appids: list[int], now: datetime, fetch=fetch_items) -> dict:
    """Check every game; a failed batch keeps each game's last confirmed values."""
    path = data_dir / FACTS_FILE
    try:
        previous = load_json(path) if path.is_file() else None
    except (OSError, ValueError, TypeError):
        previous = None
    previous = previous if valid_facts_document(previous) else None
    old = (previous or {}).get("games", {})
    games, changed, failed, stop_reason = {}, [], [], ""
    for offset in range(0, len(appids), BATCH_SIZE):
        batch = appids[offset:offset + BATCH_SIZE]
        items = {}
        if not stop_reason:
            try:
                items = fetch(batch, FACTS_REQUEST)
            except (RuntimeError, requests.RequestException, ValueError, TypeError) as error:
                if isinstance(error, SteamRateLimit):
                    stop_reason = "steam_rate_limit"
                items = None
        for appid in batch:
            key = str(appid)
            item = (items or {}).get(key)
            try:
                facts = store_facts(appid, item) if item is not None else None
            except ValueError:
                facts = None
            if facts is None:
                failed.append(appid)
                if key in old:
                    games[key] = old[key]
                continue
            games[key] = {**facts, "checked_at": timestamp(now)}
            if not same_facts(old.get(key), facts):
                changed.append(appid)
    document = facts_document(games, timestamp(now), STORE_BROWSE)
    publish = needs_publication(previous, document, now)
    write_json(path, document)
    return {"facts_game_count": len(games), "facts_changed_appids": changed, "facts_failed_appids": failed,
            "facts_publish": publish, "facts_stop_reason": stop_reason}


def refresh(data_dir: Path, *, max_games: int = 300, max_seconds: int = 300,
            now: datetime | None = None, fetch=fetch_items) -> dict:
    strict_catalog(data_dir, require_index=True)
    now = now or datetime.now(timezone.utc)
    blocked = catalog.excluded_public_appids(data_dir)
    appids = {int(load_json(path)["appid"]) for path in (data_dir / "games").glob("*.json")}
    appids = sorted(appid for appid in appids | reference_appids(data_dir) if appid not in blocked)
    status_path = data_dir / STATUS_FILE
    previous = load_json(status_path) if status_path.is_file() else {}
    checkpoints = {key: value for key, value in (previous.get("checkpoints") or {}).items()
                   if key.isdigit() and int(key) in appids and isinstance(value, dict)}
    documents = {appid: load_document(data_dir, appid) for appid in appids}

    # Leaving the catalog (or an adult-audit exclusion) removes the media file.
    removed = []
    for path in sorted((data_dir / MEDIA_DIR).glob("*.json")) if (data_dir / MEDIA_DIR).is_dir() else []:
        if not path.stem.isdigit() or int(path.stem) not in appids:
            path.unlink()
            removed.append(int(path.stem) if path.stem.isdigit() else path.stem)

    deadlines = {appid: due_at(checkpoints.get(str(appid), {}), documents[appid] is not None) for appid in appids}
    due = [appid for appid in sorted(appids, key=lambda appid: (deadlines[appid], appid)) if deadlines[appid] <= now]
    selected = due[:max_games]
    updated, unchanged, failed, stop_reason = [], [], [], ""
    start = time.monotonic()

    def fail(batch, error):
        for appid in batch:
            checkpoints[str(appid)] = {**checkpoints.get(str(appid), {}), "attempted_at": timestamp(now),
                                       "outcome": "error", "retry_at": timestamp(now + RETRY_DELAY),
                                       "error": str(error)[:300]}
            failed.append(appid)

    for offset in range(0, len(selected), BATCH_SIZE):
        if time.monotonic() - start >= max_seconds:
            stop_reason = "time_budget"
            break
        batch = selected[offset:offset + BATCH_SIZE]
        try:
            items = fetch(batch)
        except (RuntimeError, requests.RequestException, ValueError, TypeError) as error:
            fail(batch, error)
            if isinstance(error, SteamRateLimit):
                stop_reason = "steam_rate_limit"
                break
            continue
        for appid in batch:
            item = items.get(str(appid))
            if item is None:
                fail([appid], "Steam did not return a visible Store item")
                continue
            try:
                content = media_content(appid, item)
            except ValueError as error:
                fail([appid], error)
                continue
            if same_content(documents[appid], content):
                unchanged.append(appid)
            else:
                document = media_document(appid, content, timestamp(now), STORE_BROWSE)
                write_json(media_path(data_dir, appid), document)
                documents[appid] = document
                updated.append(appid)
            checkpoints[str(appid)] = {"attempted_at": timestamp(now), "checked_at": timestamp(now),
                                       "outcome": "success"}

    facts = refresh_store_facts(data_dir, appids, now, fetch)
    deadlines = {appid: due_at(checkpoints.get(str(appid), {}), documents[appid] is not None) for appid in appids}
    retry = retry_pending(appids, checkpoints)
    pending = sorted({appid for appid in appids if deadlines[appid] <= now} | set(retry))
    report = {"schema_version": SCHEMA_VERSION, "generated_at": timestamp(now), "checkpoints": checkpoints,
              "game_count": len(appids), "with_media": sum(document is not None for document in documents.values()),
              "due_count": len(due), "updated_appids": updated, "unchanged_count": len(unchanged),
              "removed_appids": removed, "failed_appids": failed, "retry_pending_appids": retry,
              "pending_count": len(pending), "complete": not pending,
              "stop_reason": stop_reason or ("game_budget" if len(due) > len(selected) else ""),
              "next_due_at": timestamp(max(min(deadlines.values()), now)) if deadlines else None,
              **facts}
    write_json(status_path, report)
    print(json.dumps({key: value for key, value in report.items() if key != "checkpoints"}, ensure_ascii=False),
          flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--max-games", type=int, default=300)
    parser.add_argument("--max-seconds", type=int, default=300)
    args = parser.parse_args()
    if not (args.data_dir / "games").is_dir():
        parser.error("data-dir must contain the existing published games directory")
    if args.max_games < 1 or args.max_seconds < 1:
        parser.error("budgets must be positive")
    refresh(args.data_dir, max_games=args.max_games, max_seconds=args.max_seconds)


if __name__ == "__main__":
    main()
