"""Compose description rules, OpenCC, and the editorial document cache."""

from __future__ import annotations

import hashlib
import html
import json
import re
from functools import lru_cache
from pathlib import Path

from opencc import OpenCC

from radar_backend.application import localized_descriptions as _application
from radar_backend.domain import localized_descriptions as _domain
from radar_backend.state import description_translations as _state

CONVERTER = OpenCC("s2twp")
HAN = _domain.HAN
KANA = _domain.KANA


def clean_description(value: object) -> str:
    return _domain.clean_description(value, re_module=re, html_module=html)


def is_chinese(value: str) -> bool:
    return _domain.is_chinese(value, han=HAN, kana=KANA)


@lru_cache(maxsize=1)
def translations() -> dict:
    return _state.read_translations(
        json.loads, Path(__file__).resolve().parents[2] / "descriptions_zh_tw.json"
    )


def description_fields(
    appid: int, english: object, traditional: object, simplified: object
) -> dict:
    return _application.description_fields(
        appid,
        english,
        traditional,
        simplified,
        clean_description=clean_description,
        is_chinese=lambda value: is_chinese(value),
        convert=lambda value: CONVERTER.convert(value),
        translations=lambda: translations(),
        fingerprint=lambda value: _domain.description_fingerprint(
            value, hash_module=hashlib
        ),
    )


def merge_description_fields(existing: dict, incoming: dict) -> dict:
    return _domain.merge_description_fields(existing, incoming)
