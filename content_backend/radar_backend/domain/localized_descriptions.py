"""Pure text, language, version, and atomic locale rules."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime

HAN = re.compile(r"[\u3400-\u9fff]")
KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa]")

DESCRIPTION_CONTENT_KEYS = (
    "short_description", "short_description_en", "short_description_language",
    "short_description_source", "description_source_url",
    "description_source_sha256", "description_translation_engine",
    "description_translation_version",
)

SOURCE_PRIORITY = {
    "steam_tchinese": 100,
    "steam_schinese_converted": 90,
    "official_zh_tw": 80,
    "official_schinese_converted": 75,
    "editorial_zh_tw": 70,
    "steam_english": 50,
}


MACHINE_SOURCES = frozenset({"steam_machine_zh_tw", "igdb_machine_zh_tw", "machine_translation"})

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
        "short_description_language": ("en" if source == "steam_english" else "zh-TW") if description else "",
        "short_description_source": source,
    }


def merge_description_fields(existing: dict, incoming: dict) -> dict:
    """Merge locale evidence atomically without losing the last valid Chinese.

    Missing Chinese must not erase valid official/editorial Chinese. Legacy
    machine translations are replaced with available English. Older queued
    snapshots must never replace newer checked description evidence.
    """
    if incoming.get("short_description_source") in MACHINE_SOURCES:
        english = clean_description(incoming.get("short_description_en"))
        incoming = {key: value for key, value in incoming.items() if key not in DESCRIPTION_CONTENT_KEYS}
        incoming.update(localized_fields(english, english, "steam_english" if english else "unavailable"))
    merged = dict(existing)
    merged.update(
        {
            key: value
            for key, value in incoming.items()
            if value is not None and value != ""
        }
    )
    checked_chinese = (
        existing.get("short_description_source") not in MACHINE_SOURCES
        and not (
            existing.get("short_description_source") == "editorial_zh_tw"
            and "short_description_source" in incoming
            and clean_description(incoming.get("short_description_en"))
            != clean_description(existing.get("short_description_en"))
        )
        and existing.get("short_description_language") in {"zh-TW", "zh-Hant"}
        and is_chinese(clean_description(existing.get("short_description")))
    )
    incoming_chinese = (
        incoming.get("short_description_language") in {"zh-TW", "zh-Hant"}
        and is_chinese(clean_description(incoming.get("short_description")))
    )
    older = False
    try:
        older = datetime.fromisoformat(str(incoming.get("description_checked_at")).replace("Z", "+00:00")) < datetime.fromisoformat(str(existing.get("description_checked_at")).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    preserve = older or (checked_chinese and (
        not incoming_chinese
        or SOURCE_PRIORITY.get(str(incoming.get("short_description_source")), 40)
        < SOURCE_PRIORITY.get(str(existing.get("short_description_source")), 40)
    ))
    if preserve:
        for key in DESCRIPTION_CONTENT_KEYS:
            if key in existing:
                merged[key] = existing[key]
            else:
                merged.pop(key, None)
        if older:
            for key in set(existing) | set(incoming):
                if not key.startswith("description_"):
                    continue
                if key in existing:
                    merged[key] = existing[key]
                else:
                    merged.pop(key, None)
    elif "short_description_source" in incoming:
        for key in DESCRIPTION_CONTENT_KEYS:
            if key in incoming or key.startswith("short_description"):
                merged[key] = incoming.get(key, "")
            else:
                merged.pop(key, None)
    return merged
