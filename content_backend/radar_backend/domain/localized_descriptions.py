"""Pure text, language, version, and atomic locale rules."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime

HAN = re.compile(r"[\u3400-\u9fff]")
KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa]")
# Chinese copy quotes Japanese titles and names in their own script: 《ゼルダの伝説》,
# 「…」, 瘋帽客（CV：小鹿なお）. Kana inside quotes and brackets or in voice credits
# do not count; elsewhere Chinese copy may still carry a few (a name, a short
# line). Only when kana are a fifth or more of the rest is the text Japanese:
# Japanese prose is a third or more kana. A voice credit names short runs
# without spaces or punctuation (family and given name may be split by one
# space), listed with 、 or ／. Same rule as the console backend.
QUOTED = re.compile(r"[《〈「『【（(\[][^《〈「『【（(\[》〉」』】）)\]\n]{0,40}[》〉」』】）)\]]")
_CAST_LABEL = r"(?:(?<![A-Za-z])[Cc][Vv](?![A-Za-z])\s*[:：.．]?|ＣＶ\s*[:：]?|(?:聲優|声優|配音)\s*[:：])"
_CAST_CHARS = r"[^\s、，,。！？!?()（）\[\]【】「」『』/／&＆:：]"
_CAST_NAME = _CAST_CHARS + r"{1,12}(?:[ 　](?!" + _CAST_LABEL + r")" + _CAST_CHARS + r"{1,8})?"
VOICE_CREDIT = re.compile(_CAST_LABEL + r"\s*" + _CAST_NAME
                          + r"(?:\s*[、，,/／&＆]\s*(?!" + _CAST_LABEL + r")" + _CAST_NAME + r"){0,9}")
KANA_SHARE = 0.2

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


def japanese(value: str, *, han=HAN, kana=KANA) -> bool:
    """Kana are a fifth or more of the Chinese and Japanese characters outside
    quotes, brackets and voice credits."""
    rest = VOICE_CREDIT.sub("", QUOTED.sub("", value))
    kanas = len(kana.findall(rest))
    return kanas > 0 and kanas >= (len(han.findall(rest)) + kanas) * KANA_SHARE


def is_chinese(value: str, *, han=HAN, kana=KANA) -> bool:
    # A Chinese-language request can silently return English or Japanese; Chinese
    # copy may still quote a Japanese title or name.
    return len(han.findall(value)) >= 4 and not japanese(value, han=han, kana=kana)


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
