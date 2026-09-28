"""Reconcile publication and progressively repair incomplete public metadata.

An accepted dispatch is not a receipt. Compare the master's accepted AppIDs
with their published records; preserve useful data on partial upstream failure.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from enrich_game import (
    SteamRateLimit, TAIPEI, _shard_in_sync, _rebuild_small_indexes, _write_json, build_record,
    excluded_public_appids, upsert_sharded, utc_now, valid_date,
)


def metadata_gaps(record: dict | None, source: dict) -> list[str]:
    if not isinstance(record, dict):
        return ['missing_record']
    gaps = []
    if record.get('release_start') != source['release_start'] or int(record.get('followers') or 0) != int(source['followers']):
        gaps.append('source_changed')
    if not (record.get('header_image') or record.get('main_capsule_image')):
        gaps.append('artwork')
    # An empty optional 2x URL is valid when Steam has been checked and has none.
    if not record.get('artwork_checked_at'):
        gaps.append('artwork_quality_unchecked')
    languages = record.get('language_support')
    if not isinstance(languages, dict) or not any(isinstance(v, bool) for v in languages.values()):
        gaps.append('languages')
    if not isinstance(record.get('tags'), list) or record.get('tags_fetch_status') == 'retry':
        gaps.append('tags')
    elif not record['tags'] and record.get('tags_fetch_status') != 'ok':
        gaps.append('tags_unchecked')
    return gaps


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return default


def reconcile(master_path: Path, data_dir: Path, max_enrich: int,
              *, max_seconds: int = 2400, cache_dir: Path | None = None) -> dict:
    started = time.monotonic()
    master = read_json(master_path, {})
    blocked = excluded_public_appids(data_dir)
    today = datetime.now(TAIPEI).date().isoformat()
    qualified = {}
    for source in master.get('games', []):
        if not isinstance(source, dict):
            continue
        try:
            appid, followers = int(source['appid']), int(source['followers'])
        except (KeyError, TypeError, ValueError):
            continue
        day = source.get('release_start')
        historical = valid_date(day) and day < today and (data_dir / 'games' / f'{appid}.json').exists()
        if appid > 0 and followers >= 5000 and valid_date(day) and appid not in blocked and (
            source.get('release_display_precision') == 'date_full' or historical):
            qualified[appid] = source
    status_path = data_dir / 'content_refresh_status.json'
    previous = read_json(status_path, {})
    failures = dict(previous.get('failures') or {})
    counts = {'attempted': 0, 'enriched': 0, 'repaired_indexes': 0}
    session = requests.Session()
    session.headers['User-Agent'] = 'GameTrendRadarContentReconcile/2.0'
    # Future launches first, then recent historical records. A broken title
    # never starves all later AppIDs in the same run.
    ordered = sorted(qualified.items(), key=lambda x: (x[1]['release_start'] < today, x[1]['release_start'], x[0]))
    for appid, source in ordered:
        path = data_dir / 'games' / f'{appid}.json'
        record = read_json(path)
        gaps = metadata_gaps(record, source)
        if gaps and counts['attempted'] < max_enrich and time.monotonic() - started < max_seconds:
            signature = f"{appid}:{source['followers']}:{source['release_start']}"
            prior_failure = failures.get(str(appid), {})
            retry_at = prior_failure.get('retry_after', '')
            if prior_failure.get('signature') == signature and retry_at and retry_at > utc_now():
                continue
            counts['attempted'] += 1
            cached_path = cache_dir / f'{appid}.json' if cache_dir else None
            cached = read_json(cached_path) if cached_path else None
            try:
                if cached and cached.get('signature') == signature:
                    enriched = cached['record']
                else:
                    enriched = build_record(session, appid=appid, followers=int(source['followers']),
                                            event_release_date=source['release_start'],
                                            follower_checked_at=source.get('follower_checked_at'),
                                            allow_historical=source['release_start'] < today and isinstance(record, dict))
                    if cached_path:
                        _write_json(cached_path, {'signature': signature, 'record': enriched})
                upsert_sharded(data_dir, enriched, source['release_start'], force=True)
                record = read_json(path)
                counts['enriched'] += 1
                failures.pop(str(appid), None)
                remaining = metadata_gaps(record, source)
                if remaining:
                    failures[str(appid)] = {'signature': signature, 'reason': ','.join(remaining),
                        'attempted_at': utc_now(), 'retry_after': (datetime.now(TAIPEI) + timedelta(hours=6)).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
                print('CONTENT_REPAIRED', appid, 'remaining', ','.join(remaining) or 'none', flush=True)
            except (RuntimeError, requests.RequestException, ValueError, OSError) as exc:
                failures[str(appid)] = {'signature': signature, 'reason': str(exc)[:220],
                    'attempted_at': utc_now(), 'retry_after': (datetime.now(TAIPEI) + timedelta(hours=6)).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
                print('CONTENT_DEFERRED', appid, type(exc).__name__, flush=True)
                if isinstance(exc, SteamRateLimit):
                    break
    # Repair all local records even after a rate limit or while remote fetches
    # are cooling down. No Steam calls are needed to repair derived files.
    for appid in qualified:
        record = read_json(data_dir / 'games' / f'{appid}.json')
        if isinstance(record, dict) and not _shard_in_sync(data_dir, record):
            upsert_sharded(data_dir, record, record['release_start'], force=True)
            counts['repaired_indexes'] += 1
    # Materialize the small browser catalog even when no game needed enrichment.
    _rebuild_small_indexes(data_dir)
    pending = {}
    for appid, source in qualified.items():
        record = read_json(data_dir / 'games' / f'{appid}.json')
        gaps = metadata_gaps(record, source)
        if gaps:
            pending[str(appid)] = gaps
        if record and not _shard_in_sync(data_dir, record):
            raise RuntimeError(f'Public catalog inconsistent for AppID {appid}')
    result = {'generated_at': utc_now(), 'qualified_master': len(qualified), **counts,
              'pending_count': len(pending), 'pending_enrichment': pending,
              'failures': {key: value for key, value in failures.items() if key in pending},
              'complete': not pending,
              'public_count': read_json(data_dir / 'index.json')['game_count']}
    _write_json(status_path, result)
    print('STEAM_CATALOG_RECONCILE', json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--master', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--max-enrich', type=int, default=60)
    parser.add_argument('--max-seconds', type=int, default=2400)
    parser.add_argument('--cache-dir', type=Path)
    args = parser.parse_args()
    if args.max_enrich < 0 or args.max_seconds <= 0:
        raise SystemExit('Invalid batch limits')
    reconcile(args.master, args.data_dir, args.max_enrich, max_seconds=args.max_seconds, cache_dir=args.cache_dir)


if __name__ == '__main__':
    main()
