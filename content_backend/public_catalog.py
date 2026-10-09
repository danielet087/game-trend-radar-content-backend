"""Small browser projection, derived exclusively from accepted AppID records."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from twitch_steam_admission import preserve_twitch_admission
from steam_player_modes import preserve_player_categories

from radar_backend.domain.release import RELEASE_FIELDS, keep_newer_release

FIELDS = (
    'appid', 'name', 'name_en', 'display_name', 'name_zh_tw', 'name_zh_cn',
    'name_zh_tw_traditional', 'name_zh_cn_traditional', 'name_en_traditional',
    'language_support', 'release_start', 'release_end', 'release_precision',
    'release_display_precision', 'release_date_timezone', 'followers',
    'follower_checked_at', 'recent_source', 'first_week_qualified_at',
    'header_image', 'header_image_2x', 'main_capsule_image', 'main_capsule_image_2x',
    'small_capsule_image', 'capsule_image', 'tags', 'genres',
    'tag_ids', 'tag_labels_zh_tw', 'genre_labels_zh_tw',
    'content_enriched_at', 'tags_fetch_status',
    'twitch_admission', 'steam_type', 'sexual_content_screened',
    'release_time_utc', 'release_date_conflict', 'official_ge5000',
    'release_timestamp_taipei_date', 'content_descriptorids',
    'release_store_date', 'release_date_normalization',
    'release_display_provider', 'release_date_verified_at',
    'categories', 'categories_source', 'categories_checked_at',
)


def write_catalog_projection(data_dir: Path, rows: list[dict], generated_at: str) -> dict:
    # Hash every accepted field, not only the number of games: metadata-only
    # changes must invalidate the release snapshot too.
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    revision = hashlib.sha256(canonical.encode()).hexdigest()[:20]
    path = data_dir / 'catalog.json'
    payload = {
        'version': 3, 'revision': revision, 'generated_at': generated_at,
        'count': len(rows),
        'games': [{key: row[key] for key in FIELDS if key in row} for row in rows],
    }
    if path.exists():
        try:
            if json.loads(path.read_text(encoding='utf-8')).get('revision') == revision:
                return {'catalog_path': 'catalog.json', 'catalog_revision': revision}
        except (OSError, ValueError, TypeError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    return {'catalog_path': 'catalog.json', 'catalog_revision': revision}
