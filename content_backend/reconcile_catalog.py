"""Repair sharded public catalog from the verified backend master.

Only add/repair qualified games. Do not delete historical releases, reset
Followers checkpoints, or silently treat a dispatched event as publication.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from enrich_game import (
    _shard_in_sync,
    excluded_public_appids,
    upsert_sharded,
    valid_date,
)


def reconcile(master_path: Path, data_dir: Path, max_enrich: int) -> dict:
    master = json.loads(master_path.read_text(encoding="utf-8"))
    blocked = excluded_public_appids(data_dir)
    qualified = {}
    for row in master.get("games", []):
        if not isinstance(row, dict):
            continue
        try:
            appid, followers = int(row["appid"]), int(row["followers"])
        except (KeyError, TypeError, ValueError):
            continue
        day = row.get("release_start")
        if (appid > 0 and followers >= 5000 and valid_date(day)
                and row.get("release_display_precision") == "date_full"
                and appid not in blocked):
            qualified[appid] = row

    checked = repaired = enriched = 0
    pending = []
    for appid, source in sorted(
        qualified.items(), key=lambda item: (item[1]["release_start"], item[0])
    ):
        checked += 1
        path = data_dir / "games" / f"{appid}.json"
        record = None
        if path.exists():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                record = None
        signature = f"{appid}:{int(source['followers'])}:{source['release_start']}"
        # Source changes need enrichment; intact existing records only need
        # deterministic local index repair (no additional Steam calls).
        needs_enrichment = (
            not isinstance(record, dict)
            or record.get("release_start") != source["release_start"]
            or int(record.get("followers") or 0) != int(source["followers"])
            or not record.get("header_image")
        )
        if needs_enrichment:
            if enriched >= max_enrich:
                pending.append(appid)
                continue
            cmd = [
                sys.executable, "content_backend/enrich_game.py",
                "--appid", str(appid),
                "--followers", str(source["followers"]),
                "--release-date", source["release_start"],
                "--data-dir", str(data_dir),
            ]
            if source.get("follower_checked_at"):
                cmd.extend(["--follower-checked-at", str(source["follower_checked_at"])])
            if record:
                cmd.append("--force")
            subprocess.run(cmd, check=True)
            enriched += 1
            repaired += 1
        elif not _shard_in_sync(data_dir, record):
            if upsert_sharded(data_dir, record, source["release_start"]):
                repaired += 1

    # Never claim full reconciliation when qualified titles remain missing.
    for appid in sorted(qualified):
        if appid in pending:
            continue
        path = data_dir / "games" / f"{appid}.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        if not _shard_in_sync(data_dir, row):
            raise RuntimeError(f"Public catalog still inconsistent for AppID {appid}")

    index = json.loads((data_dir / "index.json").read_text(encoding="utf-8"))
    legacy = json.loads((data_dir / "steam_upcoming.json").read_text(encoding="utf-8"))
    if int(index["game_count"]) != int(legacy["count"]) or (
        len(legacy["games"]) != int(index["game_count"])
    ):
        raise RuntimeError("Public index/legacy count mismatch")
    result = {
        "qualified_master": len(qualified), "checked": checked,
        "repaired": repaired, "enriched": enriched,
        "pending_enrichment": pending,
        "public_count": index["game_count"],
    }
    print("STEAM_CATALOG_RECONCILE", json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--max-enrich", type=int, default=12)
    args = parser.parse_args()
    if args.max_enrich < 0:
        raise SystemExit("max-enrich must be nonnegative")
    reconcile(args.master, args.data_dir, args.max_enrich)


if __name__ == "__main__":
    main()
