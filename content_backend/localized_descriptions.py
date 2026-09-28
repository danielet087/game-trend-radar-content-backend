"""Traditional Chinese descriptions, with explicit origin and versioned translations."""
from __future__ import annotations

import hashlib
import html
import json
import re
from functools import lru_cache
from pathlib import Path

from opencc import OpenCC

CONVERTER = OpenCC("s2twp")
HAN = re.compile(r"[\u3400-\u9fff]")
KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa]")


def clean_description(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def is_chinese(value: str) -> bool:
    # A Chinese-language request can silently return English or Japanese.
    return len(HAN.findall(value)) >= 4 and not KANA.search(value)


@lru_cache(maxsize=1)
def translations() -> dict:
    return json.loads(Path(__file__).with_name("descriptions_zh_tw.json").read_text(encoding="utf-8"))["translations"]


def description_fields(appid: int, english: object, traditional: object, simplified: object) -> dict:
    en, tw, cn = map(clean_description, (english, traditional, simplified))
    description, source = "", "unavailable"
    if is_chinese(tw):
        description, source = CONVERTER.convert(tw), "steam_tchinese"
    elif is_chinese(cn):
        description, source = CONVERTER.convert(cn), "steam_schinese_converted"
    else:
        translated = translations().get(str(appid), {})
        fingerprint = hashlib.sha256(en.encode("utf-8")).hexdigest()
        # Never carry a curated translation forward after its English source changes.
        if en and translated.get("source_sha256") == fingerprint and is_chinese(translated.get("text", "")):
            description, source = CONVERTER.convert(translated["text"]), "editorial_zh_tw"
    return {
        "short_description": description,
        "short_description_en": en,
        "short_description_language": "zh-TW" if description else "",
        "short_description_source": source,
    }


def merge_description_fields(existing: dict, incoming: dict) -> dict:
    """Keep locale and text atomic; explicit unavailable results clear stale text."""
    merged = dict(existing)
    merged.update({key: value for key, value in incoming.items() if value is not None and value != ""})
    if "short_description_source" in incoming:
        for key in ("short_description", "short_description_en", "short_description_language", "short_description_source"):
            merged[key] = incoming.get(key, "")
    elif existing.get("short_description_language") == "zh-TW":
        # Legacy queued data has no locale contract and cannot replace checked Chinese.
        for key in ("short_description", "short_description_en", "short_description_language", "short_description_source"):
            merged[key] = existing.get(key, "")
    return merged
