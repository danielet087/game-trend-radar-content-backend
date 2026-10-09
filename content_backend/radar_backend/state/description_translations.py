"""Read the versioned editorial description document at an explicit path."""

from __future__ import annotations

import json
from pathlib import Path


def read_translations(loads, path: Path) -> dict:
    return loads(path.read_text(encoding="utf-8"))["translations"]


def load_translations(path: Path) -> dict:
    return read_translations(json.loads, path)
