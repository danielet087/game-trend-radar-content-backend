"""Preserve verified release and admission evidence without storage access."""
from datetime import datetime, timezone
from radar_core.domain.twitch_admission import preserve_twitch_admission
from steam_player_modes import preserve_player_categories

RELEASE_FIELDS = (
    'release_raw', 'release_start', 'release_end', 'release_precision',
    'release_display_precision', 'release_display_provider', 'release_date_timezone',
    'release_date_basis', 'release_date_verified_at', 'release_time_utc',
    'release_time_source', 'release_timestamp_taipei_date', 'release_date_conflict',
    'post_followers_store_verified', 'post_followers_store_verified_at',
    'release_store_date', 'release_date_normalization',
)


def keep_newer_release(existing: dict, incoming: dict) -> dict:
    """Content events and master snapshots cannot roll back a newer date audit."""
    def checked_at(row):
        try:
            value = datetime.fromisoformat(str(row.get('release_date_verified_at')).replace('Z', '+00:00'))
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)
    result = preserve_player_categories(existing, preserve_twitch_admission(existing, incoming))
    # Admission is an independent source. Ordinary metadata refreshes cannot
    # erase a verified Twitch discovery, including when Followers remain low.
    if existing.get('release_display_precision') == 'date_full' and checked_at(existing) > checked_at(incoming):
        for key in RELEASE_FIELDS:
            if key in existing:
                result[key] = existing[key]
            else:
                result.pop(key, None)
    return result
