"""Refresh only the existing public games' tags and genres from the zh-TW Store."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

from enrich_game import SteamRateLimit, fetch_store_taxonomy, taxonomy_fields, upsert_sharded, utc_now


def localize(data_dir: Path, cache_dir: Path | None = None) -> dict:
    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadar/Taiwan-store-metadata"
    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((data_dir / "games").glob("*.json"))]
    completed, pending = [], []
    for index, previous in enumerate(records):
        appid = int(previous["appid"])
        cache = cache_dir / f"{appid}.json" if cache_dir else None
        payload = None
        if cache and cache.exists():
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if saved.get("appid") == appid and saved.get("language") == "tchinese":
                payload = saved["payload"]
        if payload is None:
            try:
                payload = fetch_store_taxonomy(session, appid)
            except SteamRateLimit:
                pending.extend(int(row["appid"]) for row in records[index:])
                print("TAXONOMY_RATE_LIMITED", appid, flush=True)
                break
            time.sleep(1.2)
            if payload is not None and cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"appid": appid, "language": "tchinese", "payload": payload}, ensure_ascii=False), encoding="utf-8")
        record = {**previous, **taxonomy_fields(payload, appid)}
        complete = record["tags_fetch_status"] == "ok" and record["genres_fetch_status"] == "ok"
        if complete:
            record["content_enriched_at"] = utc_now()
            record["content_enrichment_version"] = 4
            completed.append(appid)
        else:
            pending.append(appid)
        upsert_sharded(data_dir, record, previous["release_start"], force=True)
        print("TAXONOMY_UPDATED" if complete else "TAXONOMY_DEFERRED", appid, index + 1, len(records), flush=True)
    summary = {"generated_at": utc_now(), "source_language": "tchinese", "country": "TW",
               "completed": len(completed), "pending_count": len(pending), "pending_appids": pending}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    if not (args.data_dir / "games").is_dir():
        parser.error("data-dir must contain existing public AppID records")
    localize(args.data_dir, args.cache_dir)
