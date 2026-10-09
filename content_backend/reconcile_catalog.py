"""Reconcile publication and progressively repair incomplete public metadata.

An accepted dispatch is not a receipt. Compare the master's accepted AppIDs
with their published records; preserve useful data on partial upstream failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The historical root-module CLI uses the same top-level helper imports as direct execution.
if __package__ == "content_backend":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from public_catalog import keep_newer_release
from steam_player_modes import has_verified_categories
from twitch_steam_admission import aware_time, is_twitch_qualified, normalize_twitch_admission
from enrich_game import (
    SteamRateLimit, TAIPEI, _shard_in_sync, _rebuild_small_indexes, _write_json, build_record,
    excluded_public_appids, upsert_sharded, utc_now, valid_date,
    refresh_player_categories,
)


from radar_backend.domain.catalog import metadata_gaps
from radar_backend.application.reconciliation import ReconcilePorts, reconcile as _reconcile
from radar_backend.state.json_documents import read_json

def reconcile(master_path: Path, data_dir: Path, max_enrich: int,
              *, max_seconds: int = 2400, cache_dir: Path | None = None) -> dict:
    ports = ReconcilePorts(read_json, _write_json, excluded_public_appids,
                           build_record, refresh_player_categories, upsert_sharded,
                           _shard_in_sync, _rebuild_small_indexes, requests.Session,
                           time.monotonic, datetime.now, utc_now,
                           requests.RequestException, SteamRateLimit)
    return _reconcile(master_path, data_dir, max_enrich, ports=ports,
                      max_seconds=max_seconds, cache_dir=cache_dir)


def main():
    from radar_backend.jobs.reconcile_content import main as run_job
    run_job(reconcile=reconcile)


if __name__ == '__main__':
    main()
