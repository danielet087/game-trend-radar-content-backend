"""JSON file storage with explicit strict and legacy-tolerant reads."""
from __future__ import annotations
import json
from pathlib import Path


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))

def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return default


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
