"""Refresh the Steam versions of public console games.

python content_backend/refresh_references.py --data-dir /path/to/frontend/data

Writes data/steam_references.json and data/steam_reference_status.json only.
A console game that names one Steam product (and is the only public console
game naming it under another title) gets that product's Store facts, checked
every three days and at once when the console link changes. Products already
in the Steam catalog, excluded by the adult audit, failing the content screen
or titled as something else get no reference row.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from opencc import OpenCC
import requests

from localized_descriptions import description_fields
from radar_backend.adapters import steam_store
from radar_backend.domain.content import APPDETAILS, STORE_BROWSE, taxonomy_fields
from radar_backend.domain.steam_references import (CONSOLE_FILE, REFERENCES_FILE, RETRY_DELAY, STATUS_FILE,
                                                   console_links, current_records, due_at, link_signature,
                                                   reference_record, references_document, timestamp,
                                                   title_match, valid_document)
from radar_backend.domain.steam_taxonomy import TAG_LIST
from radar_backend.publication import catalog
from radar_backend.publication.content_snapshot import strict_catalog
from radar_backend.state.json_documents import load_json, write_json
from radar_core.domain.mature_content import decide_content

BATCH_SIZE = 50
BROWSE_REQUEST = {"include_basic_info": True, "include_release": True, "include_assets": True,
                  "include_supported_languages": True, "include_categories": True, "include_tag_count": 20}
LANGUAGES = ("english", "tchinese", "schinese")
# Steam's genre IDs with the English names the catalog already uses.
GENRES = {1: "Action", 2: "Strategy", 3: "RPG", 4: "Casual", 9: "Racing", 18: "Sports", 23: "Indie",
          25: "Adventure", 28: "Simulation", 29: "Massively Multiplayer", 37: "Free to Play",
          70: "Early Access", 73: "Violent", 74: "Gore"}
OPENCC = OpenCC("s2t")


class SteamClient:
    """Serial Store requests; a 429 raises SteamRateLimit and ends the run."""

    def __init__(self, session=None, *, sleep=time.sleep, interval: float = 1.5):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = "GameTrendRadar/steam-references"
        self.sleep, self.interval, self.requests = sleep, interval, 0
        self._tags: dict[str, dict[int, str]] = {}

    def _get(self, url: str, params: dict) -> dict:
        if self.requests:
            self.sleep(self.interval)
        self.requests += 1
        return steam_store.request_json(self.session, url, params=params, attempts=2, sleep=self.sleep)

    def browse(self, appids: list[int], language: str) -> dict[int, dict]:
        rows = {}
        for start in range(0, len(appids), BATCH_SIZE):
            payload = {"ids": [{"appid": appid} for appid in appids[start:start + BATCH_SIZE]],
                       "context": {"country_code": "TW", "language": language, "steam_realm": 1},
                       "data_request": BROWSE_REQUEST}
            data = self._get(STORE_BROWSE, {"input_json": json.dumps(payload, separators=(",", ":"))})
            items = (data.get("response") or {}).get("store_items")
            if not isinstance(items, list):
                raise RuntimeError("Malformed Steam Store Browse response")
            rows.update({row["appid"]: row for row in items if isinstance(row, dict) and type(row.get("appid")) is int})
        return rows

    def details(self, appid: int, language: str = "tchinese") -> dict:
        data = self._get(APPDETAILS, {"appids": appid, "cc": "TW", "l": language})
        row = data.get(str(appid)) or {}
        return row["data"] if row.get("success") is True and isinstance(row.get("data"), dict) else {}

    def tag_names(self, language: str) -> dict[int, str]:
        if language not in self._tags:
            data = self._get(TAG_LIST, {"language": language})
            self._tags[language] = {row["tagid"]: row["name"] for row in (data.get("response") or {}).get("tags", [])
                                    if isinstance(row, dict) and type(row.get("tagid")) is int
                                    and isinstance(row.get("name"), str)}
        return self._tags[language]


def store_taxonomy(client, appid: int, traditional: dict, details: dict) -> dict | None:
    """Tags from Store Browse and genres from appdetails, as the catalog stores them."""
    try:
        english, chinese = client.tag_names("english"), client.tag_names("tchinese")
    except (RuntimeError, requests.RequestException, ValueError, TypeError):
        return None
    ids = list(dict.fromkeys(tag for tag in traditional.get("tagids") or [] if type(tag) is int and tag > 0))[:20]
    if not ids or not chinese or any(tag not in chinese for tag in ids):
        return None
    tags = [english.get(tag) or f"steam-tag:{tag}" for tag in ids]
    result = {"tags": tags, "tag_ids": dict(zip(tags, ids)),
              "tag_labels_zh_tw": {name: chinese[tag] for name, tag in zip(tags, ids)},
              "tags_source": STORE_BROWSE, "tag_labels_source": TAG_LIST + "?language=tchinese",
              "genres": None, "genre_labels_zh_tw": {}}
    genres = details.get("genres")
    if isinstance(genres, list):
        result["genres"] = []
        for row in genres:
            if not isinstance(row, dict) or not str(row.get("id", "")).isdigit() or not isinstance(row.get("description"), str):
                continue
            name = GENRES.get(int(row["id"])) or f"steam-genre:{row['id']}"
            if name not in result["genres"]:
                result["genres"].append(name)
                result["genre_labels_zh_tw"][name] = row["description"]
        result["genres_source"] = APPDETAILS + f"?appids={appid}&cc=TW&l=tchinese"
    return result


def write_compact(path: Path, document: dict) -> None:
    """Without indentation, like catalog.json: every list page reads this file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def _load_references(path: Path) -> dict | None:
    try:
        document = load_json(path) if path.is_file() else None
    except (OSError, ValueError, TypeError):
        return None
    return document if valid_document(document) else None


def screen(appid: int, english: dict, details: dict):
    """The Steam radar's sexual-content screen, on the same AppID's own data."""
    screened = {**english, "appid": appid, "content_descriptorids": list(english.get("content_descriptorids") or [])}
    if not isinstance(screened.get("tags"), list):
        screened["tags"] = [{"tagid": tag} for tag in english.get("tagids") or []]
    return decide_content(screened, details)


def evaluate(client, appid: int, links: list[dict], items: dict[str, dict], now: datetime) -> tuple[str, str, dict | None]:
    """(outcome, reason, record) for one product; raises on transport errors."""
    english = items.get("english")
    if not isinstance(english, dict) or english.get("success", 1) != 1 or english.get("visible") is False:
        return "unavailable", "Steam did not return a visible Taiwan Store item", None
    matches = [title_match(row["name_en"], english.get("name")) for row in links]
    if not all(matches):
        return "title_mismatch", f"Store title {english.get('name')!r} names another product", None
    details = client.details(appid)
    if not details:
        return "error", "Steam appdetails unavailable", None
    if details.get("type") != "game" or details.get("steam_appid") != appid:
        return "not_a_game", f"Steam type is {details.get('type')!r}", None
    decision = screen(appid, english, details)
    if decision.status != "screened":
        return ("excluded" if decision.status == "excluded" else "content_review"), decision.reason, None
    checked_at = timestamp(now)
    traditional, simplified = items.get("tchinese") or {}, items.get("schinese") or {}
    descriptions = description_fields(
        appid, (english.get("basic_info") or {}).get("short_description"),
        (traditional.get("basic_info") or {}).get("short_description") or details.get("short_description"),
        (simplified.get("basic_info") or {}).get("short_description"))
    taxonomy = taxonomy_fields(store_taxonomy(client, appid, traditional, details), appid, checked_at)
    best = "exact" if "exact" in matches else "subtitle" if "subtitle" in matches else "contained"
    try:
        record = reference_record(appid, links, english=english, traditional=traditional, simplified=simplified,
                                  details=details, taxonomy=taxonomy, descriptions=descriptions, match=best,
                                  checked_at=checked_at, now=now, convert=OPENCC.convert)
    except ValueError as error:
        return "unavailable", str(error), None
    return "accepted", decision.reason, record


def refresh(data_dir: Path, *, max_games: int = 60, max_seconds: int = 150, now: datetime | None = None,
            client=None, monotonic=time.monotonic) -> dict:
    strict_catalog(data_dir, require_index=True)
    now = now or datetime.now(timezone.utc)
    blocked = catalog.excluded_public_appids(data_dir)
    steam = {int(load_json(path)["appid"]) for path in (data_dir / "games").glob("*.json")}
    references_path, status_path = data_dir / REFERENCES_FILE, data_dir / STATUS_FILE
    previous = _load_references(references_path)
    try:
        prior_status = load_json(status_path) if status_path.is_file() else {}
    except (OSError, ValueError, TypeError):
        prior_status = {}
    try:
        links = console_links(load_json(data_dir / CONSOLE_FILE))
    except (OSError, ValueError, TypeError):
        # Without the console catalog nothing is decided: keep what is published.
        report = {"schema_version": 1, "generated_at": timestamp(now), "checkpoints": prior_status.get("checkpoints") or {},
                  "stop_reason": "console_catalog_unavailable", "changed": False,
                  "reference_count": len((previous or {}).get("games", []))}
        write_json(status_path, report)
        print(json.dumps({key: value for key, value in report.items() if key != "checkpoints"}), flush=True)
        return report
    candidates = {appid: rows for appid, rows in links.items() if appid not in steam and appid not in blocked}
    records = {row["appid"]: row for row in current_records((previous or {}).get("games", []), links, steam, blocked)}
    checkpoints = {key: value for key, value in (prior_status.get("checkpoints") or {}).items()
                   if key.isdigit() and int(key) in candidates and isinstance(value, dict)}
    deadlines = {appid: due_at(checkpoints.get(str(appid), {}), rows, appid in records)
                 for appid, rows in candidates.items()}
    due = sorted((appid for appid in candidates if deadlines[appid] <= now), key=lambda appid: (deadlines[appid], appid))
    selected = due[:max_games]
    client = client or SteamClient()
    start, stop_reason, outcomes = monotonic(), "", {}
    items: dict[int, dict[str, dict]] = {appid: {} for appid in selected}

    def fail(appid: int, reason: str) -> None:
        checkpoints[str(appid)] = {**checkpoints.get(str(appid), {}), "links": link_signature(candidates[appid]),
                                   "attempted_at": timestamp(now), "outcome": "error",
                                   "retry_at": timestamp(now + RETRY_DELAY), "reason": reason[:300]}
        outcomes[appid] = "error"

    try:
        for language in LANGUAGES if selected else ():
            for appid, item in client.browse(selected, language).items():
                if appid in items:
                    items[appid][language] = item
    except steam_store.SteamRateLimit:
        selected, stop_reason = [], "steam_rate_limit"
    except (RuntimeError, requests.RequestException, ValueError, TypeError) as error:
        for appid in selected:
            fail(appid, f"Store Browse failed: {error}")
        selected = []
    for appid in selected:
        if monotonic() - start >= max_seconds:
            stop_reason = "time_budget"
            break
        try:
            outcome, reason, record = evaluate(client, appid, candidates[appid], items[appid], now)
        except steam_store.SteamRateLimit:
            stop_reason = "steam_rate_limit"
            break
        except (RuntimeError, requests.RequestException, ValueError, TypeError) as error:
            fail(appid, str(error))
            continue
        if outcome == "error":
            fail(appid, reason)
            continue
        outcomes[appid] = outcome
        checkpoints[str(appid)] = {"links": link_signature(candidates[appid]), "attempted_at": timestamp(now),
                                   "checked_at": timestamp(now), "outcome": outcome,
                                   **({"reason": reason[:300]} if reason else {})}
        if record is not None:
            records[appid] = record
        else:
            records.pop(appid, None)
    document = references_document(list(records.values()), timestamp(now))
    changed = previous is None or previous["games"] != document["games"]
    if changed:
        write_compact(references_path, document)
    deadlines = {appid: due_at(checkpoints.get(str(appid), {}), rows, appid in records)
                 for appid, rows in candidates.items()}
    pending = sorted(appid for appid in candidates if deadlines[appid] <= now)
    counts: dict[str, int] = {}
    for checkpoint in checkpoints.values():
        counts[checkpoint.get("outcome", "unknown")] = counts.get(checkpoint.get("outcome", "unknown"), 0) + 1
    report = {"schema_version": 1, "generated_at": timestamp(now), "checkpoints": checkpoints,
              "linked_count": len(links), "candidate_count": len(candidates), "reference_count": len(records),
              "due_count": len(due), "checked_appids": sorted(outcomes), "outcome_counts": dict(sorted(counts.items())),
              "pending_count": len(pending), "complete": not pending, "changed": changed,
              "requests": getattr(client, "requests", None),
              "stop_reason": stop_reason or ("game_budget" if len(due) > len(selected) else ""),
              "next_due_at": timestamp(max(min(deadlines.values()), now)) if deadlines else None}
    write_json(status_path, report)
    print(json.dumps({key: value for key, value in report.items() if key != "checkpoints"}, ensure_ascii=False),
          flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--max-games", type=int, default=60)
    parser.add_argument("--max-seconds", type=int, default=150)
    args = parser.parse_args()
    if not (args.data_dir / "games").is_dir():
        parser.error("data-dir must contain the existing published games directory")
    if args.max_games < 1 or args.max_seconds < 1:
        parser.error("budgets must be positive")
    refresh(args.data_dir, max_games=args.max_games, max_seconds=args.max_seconds)


if __name__ == "__main__":
    main()
