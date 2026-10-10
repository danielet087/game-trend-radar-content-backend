"""Compatibility API and CLI; content rules and use cases live in radar_backend."""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

# The historical root-module CLI uses the same top-level helper imports as direct execution.
if __package__ == "content_backend":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from opencc import OpenCC
from public_catalog import keep_newer_release, write_catalog_projection
from localized_descriptions import description_fields, merge_description_fields
from steam_taxonomy import TAG_LIST, parse_store_taxonomy, preserve_taxonomy
from steam_player_modes import category_fields, has_verified_categories
from twitch_steam_admission import (
    TW_STORE_DATE_AUTHORITY, TW_STORE_DATE_PROVIDER, is_twitch_qualified,
    normalize_twitch_admission, resolve_store_release_day,
)

LOG = logging.getLogger(__name__)


from radar_backend.adapters import steam_store as _store
from radar_backend.application.enrichment import EnrichmentPorts, build_record as _build_record, refresh_player_categories as _refresh_categories
from radar_backend.domain.content import (
    STORE_BROWSE, APPDETAILS, STORE_PAGE, ASSET_BASE, TAIPEI, HAN,
    ALLOWED_ASSET_HOSTS, LANG_IDS, OTHER_LANGUAGE_NAMES, SEXUAL_CONTENT_IDS, EXPLICIT_DESC,
    is_explicit_sex_game, exact_store_display_date, valid_date,
    steam_asset_url, read_support, official_chinese_title,
    taxonomy_fields as _taxonomy_fields,
)
from radar_backend.publication.catalog import (
    _update_month_file, _rebuild_small_indexes, _shard_in_sync,
    excluded_public_appids, upsert_sharded,
)
from radar_backend.state.json_documents import write_json as _write_json
from radar_backend.domain.catalog import official_followers_at_least, project_follower_evidence

SteamRateLimit = _store.SteamRateLimit
OPENCC = OpenCC("s2t")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def request_json(session, url, *, params: dict, attempts: int = 4) -> dict:
    return _store.request_json(session, url, params=params, attempts=attempts, sleep=time.sleep)


def browse_one(session, appid: int, language: str) -> dict:
    return _store.browse_one(session, appid, language, request=request_json)


def appdetails(session, appid: int, language: str = "tchinese") -> dict:
    return _store.appdetails(session, appid, language, request=request_json, sleep=time.sleep)


def fetch_taxonomy_from_api(session, appid: int, names: dict) -> dict | None:
    return _store.fetch_taxonomy_from_api(session, appid, names, request=request_json,
                                        browse=browse_one, details_lookup=appdetails)


def fetch_store_taxonomy(session, appid: int) -> dict | None:
    return _store.fetch_store_taxonomy(session, appid, request=request_json,
                                      fallback=fetch_taxonomy_from_api)


def taxonomy_fields(payload: dict | None, appid: int) -> dict:
    return _taxonomy_fields(payload, appid, utc_now())


def build_record(session, *, appid: int, followers: int | None, event_release_date: str,
                 follower_checked_at: str | None, allow_historical: bool = False,
                 twitch_admission: dict | None = None, follower_status: str | None = None,
                 follower_unavailable_at: str | None = None) -> dict:
    ports = EnrichmentPorts(browse_one, appdetails, fetch_store_taxonomy,
                            description_fields, OPENCC.convert, time.sleep, utc_now,
                            lambda: datetime.now(timezone.utc))
    return _build_record(session, ports=ports, appid=appid, followers=followers,
                         event_release_date=event_release_date,
                         follower_checked_at=follower_checked_at,
                         allow_historical=allow_historical, twitch_admission=twitch_admission,
                         follower_status=follower_status, follower_unavailable_at=follower_unavailable_at)

def refresh_player_categories(session: requests.Session, appid: int) -> dict:
    return _refresh_categories(session, appid, browse=browse_one, details=appdetails, utc_now=utc_now)


def upsert_document(path: Path, record: dict, event_release_date: str, *, force: bool = False) -> bool:
    doc = json.loads(path.read_text(encoding="utf-8"))
    games = doc.get("games")
    if not isinstance(games, list):
        raise RuntimeError(f"Invalid games array in {path}")
    appid = int(record["appid"])
    existing = next(
        (g for g in games if int(g.get("appid", -1)) == appid), None
    )
    record = keep_newer_release(existing or {}, record)
    if ('twitch_admission' in record or record.get('followers') is None) and not is_twitch_qualified(record):
        raise RuntimeError(f'AppID {appid} has an invalid Twitch-source Steam qualification')
    signature = f"{appid}:{record['followers']}:{record['release_start']}"
    if existing:
        already = (
            existing.get("content_enrichment_signature") == signature
            and existing.get("header_image")
            and isinstance(existing.get("language_support"), dict)
            and isinstance(existing.get("tags"), list)
            and (official_followers_at_least(existing, 5000) or is_twitch_qualified(existing))
            and existing.get('twitch_admission') == record.get('twitch_admission')
            and all(existing.get(key) == record.get(key) for key in ("follower_status", "follower_unavailable_at"))
            and (not has_verified_categories(record) or (
                has_verified_categories(existing) and existing['categories'] == record['categories']))
        )
        if already and not force:
            return False
        existing.update(project_follower_evidence(record, preserve_taxonomy(existing, merge_description_fields(existing, record))))
        for key in ("follower_status", "follower_unavailable_at"):
            if key not in record and record.get("followers") is not None:
                existing.pop(key, None)
        existing["content_enrichment_signature"] = signature
    else:
        row = dict(record)
        row["content_enrichment_signature"] = signature
        games.append(row)
    games.sort(key=lambda g: (
        str(g.get("release_start") or "9999-12-31"),
        int(g.get("appid", 0)),
    ))
    doc["games"] = games
    doc["count"] = len(games)
    doc["generated_at"] = utc_now()
    path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True

def main() -> None:
    from radar_backend.jobs.enrich_content import main as run_job
    run_job(build_record=build_record, publish=upsert_sharded)



if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
