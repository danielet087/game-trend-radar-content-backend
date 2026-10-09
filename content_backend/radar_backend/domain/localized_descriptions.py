"""Pure text, language, version, and atomic locale rules."""

from __future__ import annotations

import hashlib
import html
import re

HAN = re.compile(r"[\u3400-\u9fff]")
KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa]")


def clean_description(value: object, *, re_module=re, html_module=html) -> str:
    if not isinstance(value, str):
        return ""
    text = re_module.sub(r"<[^>]+>", " ", value)
    return re_module.sub(r"\s+", " ", html_module.unescape(text)).strip()


def is_chinese(value: str, *, han=HAN, kana=KANA) -> bool:
    # A Chinese-language request can silently return English or Japanese.
    return len(han.findall(value)) >= 4 and not kana.search(value)


def description_fingerprint(english: str, *, hash_module=hashlib) -> str:
    return hash_module.sha256(english.encode("utf-8")).hexdigest()


def localized_fields(english: str, description: str, source: str) -> dict:
    return {
        "short_description": description,
        "short_description_en": english,
        "short_description_language": "zh-TW" if description else "",
        "short_description_source": source,
    }


def merge_description_fields(existing: dict, incoming: dict) -> dict:
    """Keep locale and text atomic; explicit unavailable results clear stale text."""
    merged = dict(existing)
    merged.update(
        {
            key: value
            for key, value in incoming.items()
            if value is not None and value != ""
        }
    )
    if "short_description_source" in incoming:
        for key in (
            "short_description",
            "short_description_en",
            "short_description_language",
            "short_description_source",
        ):
            merged[key] = incoming.get(key, "")
    elif existing.get("short_description_language") == "zh-TW":
        # Legacy queued data has no locale contract and cannot replace checked Chinese.
        for key in (
            "short_description",
            "short_description_en",
            "short_description_language",
            "short_description_source",
        ):
            merged[key] = existing.get(key, "")
    return merged
