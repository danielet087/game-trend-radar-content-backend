"""Refresh descriptions only; preserve eligibility, followers, dates and artwork.

python content_backend/localize_descriptions.py --data-dir /path/to/frontend/data
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import requests

from enrich_game import STORE_BROWSE, request_json, upsert_sharded, utc_now
from localized_descriptions import description_fields


def fetch_locales(appids: list[int], cache_dir: Path | None = None) -> dict:
    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadar/description-localization"
    result = {}
    for language in ("tchinese", "schinese"):
        cache = cache_dir / f"{language}.json" if cache_dir else None
        if cache and cache.exists():
            rows = json.loads(cache.read_text(encoding="utf-8"))
        else:
            rows = {}
            for start in range(0, len(appids), 25):
                payload = {
                    "ids": [{"appid": appid} for appid in appids[start:start + 25]],
                    "context": {"country_code": "TW", "language": language, "steam_realm": 1},
                    "data_request": {"include_basic_info": True},
                }
                data = request_json(session, STORE_BROWSE, params={"input_json": json.dumps(payload, separators=(",", ":"))})
                rows.update({str(row["appid"]): row for row in data.get("response", {}).get("store_items", [])})
                time.sleep(1)
            if cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        missing = set(map(str, appids)) - rows.keys()
        if missing:
            raise RuntimeError(f"Incomplete {language} response; missing {len(missing)} AppIDs")
        result[language] = rows
    return result


def localize(data_dir: Path, cache_dir: Path | None = None) -> dict:
    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((data_dir / "games").glob("*.json"))]
    locales = fetch_locales([int(row["appid"]) for row in records], cache_dir)
    counts = Counter()
    for previous in records:
        appid = int(previous["appid"])
        descriptions = [(locales[language][str(appid)].get("basic_info") or {}).get("short_description")
                        for language in ("tchinese", "schinese")]
        record = {**previous, **description_fields(appid, previous.get("short_description_en") or previous.get("short_description"), *descriptions),
                  "description_checked_at": utc_now()}
        upsert_sharded(data_dir, record, previous["release_start"], force=True)
        counts[record["short_description_source"]] += 1
    print(json.dumps(dict(counts), ensure_ascii=False), flush=True)
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    if not (args.data_dir / "games").is_dir():
        parser.error("data-dir must contain the existing published games directory")
    localize(args.data_dir, args.cache_dir)


if __name__ == "__main__":
    main()
