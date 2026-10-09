"""Compatibility API for accepted-record release and browser projections."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from twitch_steam_admission import preserve_twitch_admission
from steam_player_modes import preserve_player_categories

from radar_backend.domain.catalog_projection import FIELDS
from radar_backend.domain.release import (
    RELEASE_FIELDS,
    keep_newer_release as _keep_newer_release,
)
from radar_backend.state.catalog_projection import (
    write_catalog_projection as _write_catalog_projection,
)


def keep_newer_release(existing: dict, incoming: dict) -> dict:
    return _keep_newer_release(
        existing, incoming, preserve_categories=preserve_player_categories
    )


def write_catalog_projection(
    data_dir: Path, rows: list[dict], generated_at: str
) -> dict:
    return _write_catalog_projection(
        data_dir, rows, generated_at, fields=FIELDS, json_codec=json, hash_codec=hashlib
    )
