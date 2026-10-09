"""Pure Steam taxonomy parsing and retry-preservation rules."""

from __future__ import annotations

import html
import json
import re
from urllib.parse import unquote, urlsplit

TAG_LIST = "https://api.steampowered.com/IStoreService/GetTagList/v1/"
STORE_PAGE = "https://store.steampowered.com/app/{appid}/"


def text(value: str, *, html_module=html, re_module=re) -> str:
    html, re = html_module, re_module
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", value))).strip()


def parse_store_taxonomy(
    body: str,
    appid: int,
    tag_names: dict[int, str],
    *,
    html_module=html,
    json_module=json,
    re_module=re,
    text_fn=text,
    unquote_fn=unquote,
    urlsplit_fn=urlsplit,
) -> dict | None:
    html, json, re = html_module, json_module, re_module
    text, unquote, urlsplit = text_fn, unquote_fn, urlsplit_fn
    # Never label an English fallback page or a different game's tags as zh-TW.
    if not re.search(r'<html\b[^>]*\blang=["\']zh-tw["\']', body, re.I):
        return None
    match = re.search(r"InitAppTagModal\s*\(\s*(\d+)\s*,\s*", body)
    if not match or int(match[1]) != appid:
        return None
    try:
        rows, _ = json.JSONDecoder().raw_decode(body[match.end() :])
    except (ValueError, TypeError):
        return None
    if not isinstance(rows, list):
        return None
    tags, ids, labels = [], {}, {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or type(row.get("tagid")) is not int
            or row["tagid"] <= 0
        ):
            continue
        if row.get("browseable") is False or not isinstance(row.get("name"), str):
            continue
        tag_id = row["tagid"]
        label = text(row["name"])
        if not label or tag_id in ids.values():
            continue
        # Existing English URLs remain valid; a brand-new tag still gets a stable ID.
        name = tag_names.get(tag_id) or f"steam-tag:{tag_id}"
        tags.append(name)
        ids[name], labels[name] = tag_id, label
        if len(tags) == 20:
            break
    if rows and not tags:
        return None
    genres, genre_labels = None, {}
    section = re.search(
        r'<div\b[^>]*\bid=["\']genresAndManufacturer["\'][^>]*>(.*?)<br\s*/?>\s*<div',
        body,
        re.I | re.S,
    )
    # Stop at the first line break following the genre label, before publisher links.
    if section:
        genre_line = re.search(
            r"<b>\s*類型\s*[:：]\s*</b>(.*?)<br\s*/?>", section[1] + "<br>", re.I | re.S
        )
        if genre_line:
            genres = []
            for href, raw in re.findall(
                r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                genre_line[1],
                re.I | re.S,
            ):
                url = urlsplit(html.unescape(href))
                if url.hostname != "store.steampowered.com" or not url.path.startswith(
                    "/genre/"
                ):
                    continue
                name, label = unquote(url.path.split("/")[2]).strip(), text(raw)
                if name and label and name not in genres:
                    genres.append(name)
                    genre_labels[name] = label
    return {
        "tags": tags,
        "tag_ids": ids,
        "tag_labels_zh_tw": labels,
        "genres": genres,
        "genre_labels_zh_tw": genre_labels,
    }


def preserve_taxonomy(existing: dict, incoming: dict) -> dict:
    result = dict(incoming)
    for field, status, related in (
        (
            "tags",
            "tags_fetch_status",
            (
                "tag_ids",
                "tag_labels_zh_tw",
                "tag_labels_language",
                "tags_source",
                "tag_labels_source",
                "tags_checked_at",
            ),
        ),
        (
            "genres",
            "genres_fetch_status",
            (
                "genre_labels_zh_tw",
                "genre_labels_language",
                "genres_source",
                "genres_checked_at",
            ),
        ),
    ):
        if incoming.get(status) == "retry" and field in existing:
            for name in (field, *related):
                if name in existing:
                    result[name] = existing[name]
                else:
                    result.pop(name, None)
    return result
