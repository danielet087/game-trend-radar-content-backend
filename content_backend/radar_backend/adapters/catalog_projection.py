"""Compose browser projection rules with their JSON file store."""

import hashlib
import json
from pathlib import Path

from radar_backend.domain.catalog_projection import FIELDS
from radar_backend.state.catalog_projection import (
    write_catalog_projection as _write_catalog_projection,
)


def write_catalog_projection(
    data_dir: Path, rows: list[dict], generated_at: str
) -> dict:
    return _write_catalog_projection(
        data_dir, rows, generated_at, fields=FIELDS, json_codec=json, hash_codec=hashlib
    )
