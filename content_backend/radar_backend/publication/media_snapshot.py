"""Freeze refreshed media files, store facts and Steam references; replay them against the latest data."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from radar_core.publication import snapshot_revision
from radar_backend.domain.steam_media import (MANIFEST, MEDIA_DIR, MINIMUM_TIME, STATUS_FILE, aware_time,
                                              due_at, retry_pending, timestamp, valid_document)
from radar_backend.domain.steam_store_facts import FACTS_FILE
from radar_backend.domain.steam_store_facts import aware_time as facts_time
from radar_backend.domain.steam_store_facts import valid_document as valid_facts_document
from radar_backend.domain.steam_references import (CONSOLE_FILE, REFERENCES_FILE, console_links, current_records,
                                                   references_document)
from radar_backend.domain.steam_references import STATUS_FILE as REFERENCE_STATUS_FILE
from radar_backend.domain.steam_references import aware_time as reference_time
from radar_backend.domain.steam_references import valid_document as valid_references_document
from radar_backend.publication import catalog
from radar_backend.publication.content_snapshot import strict_catalog
from radar_backend.state.json_documents import load_json, write_json

OWNED_PATHS = (f"data/{MEDIA_DIR}/", f"data/{STATUS_FILE}", f"data/{MANIFEST}", f"data/{FACTS_FILE}",
               f"data/{REFERENCES_FILE}", f"data/{REFERENCE_STATUS_FILE}")


def _load_references(path: Path) -> dict | None:
    try:
        document = load_json(path) if path.is_file() else None
    except (OSError, ValueError, TypeError):
        return None
    return document if valid_references_document(document) else None


def _load_reference_status(path: Path) -> dict | None:
    try:
        document = load_json(path) if path.is_file() else None
    except (OSError, ValueError, TypeError):
        return None
    return (document if isinstance(document, dict) and document.get("schema_version") == 1
            and isinstance(document.get("checkpoints"), dict) else None)


def _write_compact(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def _load_facts(path: Path) -> dict | None:
    try:
        document = load_json(path) if path.is_file() else None
    except (OSError, ValueError, TypeError):
        return None
    return document if valid_facts_document(document) else None


def freeze(frontend: Path, *, input_revision: str) -> dict:
    data = frontend / "data"
    strict_catalog(data, require_index=True)
    status = load_json(data / STATUS_FILE)
    if status.get("schema_version") != 1 or not isinstance(status.get("checkpoints"), dict):
        raise RuntimeError("Malformed media refresh status")
    documents = []
    for appid in status.get("updated_appids", []):
        document = load_json(data / MEDIA_DIR / f"{int(appid)}.json")
        if not valid_document(document, int(appid)):
            raise RuntimeError(f"Refreshed media file for AppID {appid} is malformed")
        documents.append(document)
    store = None
    if status.get("facts_publish"):
        store = _load_facts(data / FACTS_FILE)
        if store is None:
            raise RuntimeError("Refreshed store facts are malformed")
    reference_status = _load_reference_status(data / REFERENCE_STATUS_FILE)
    references = None
    if (reference_status or {}).get("changed"):
        references = _load_references(data / REFERENCES_FILE)
        if references is None:
            raise RuntimeError("Refreshed Steam references are malformed")
    return {"schema_version": 1, "kind": "steam_media", "input_revision": input_revision,
            "prepared_at": timestamp(datetime.now(timezone.utc)), "documents": documents,
            "store_facts": store, "status": status, "references": references,
            "reference_status": reference_status}


class FrozenMediaPublication:
    def __init__(self, frozen: dict):
        if frozen.get("schema_version") != 1 or frozen.get("kind") != "steam_media":
            raise RuntimeError("Malformed frozen media batch")
        if (not isinstance(frozen.get("documents"), list) or aware_time(frozen.get("prepared_at")) is None
                or not isinstance((frozen.get("status") or {}).get("checkpoints"), dict)):
            raise RuntimeError("Invalid frozen media evidence")
        for document in frozen["documents"]:
            if not isinstance(document, dict) or not valid_document(document, document.get("appid")):
                raise RuntimeError("Frozen media batch contains a malformed document")
        if frozen.get("store_facts") is not None and not valid_facts_document(frozen["store_facts"]):
            raise RuntimeError("Frozen media batch contains malformed store facts")
        if frozen.get("references") is not None and not valid_references_document(frozen["references"]):
            raise RuntimeError("Frozen media batch contains malformed Steam references")
        reference_status = frozen.get("reference_status")
        if reference_status is not None and (not isinstance(reference_status, dict)
                                             or not isinstance(reference_status.get("checkpoints"), dict)):
            raise RuntimeError("Frozen media batch contains a malformed Steam reference status")
        self.frozen = frozen
        self.payload_revision = snapshot_revision(frozen)
        self.result = {}

    def _references(self, data: Path, steam: set[int], blocked: set[int]) -> tuple[set[int], str]:
        """Write the newer reference file, keep only rows the latest data still justifies."""
        path = data / REFERENCES_FILE
        existing = _load_references(path)
        document, outcome = existing, "unchanged"
        incoming = self.frozen.get("references")
        if incoming is not None:
            if existing is not None and reference_time(existing["generated_at"]) > reference_time(incoming["generated_at"]):
                outcome = "superseded"
            else:
                document, outcome = incoming, "written"
        try:
            links = console_links(load_json(data / CONSOLE_FILE))
        except (OSError, ValueError, TypeError):
            links = None  # Without the console catalog, keep what is published.
        if document is not None and links is not None:
            kept = current_records(document["games"], links, steam, blocked)
            if kept != document["games"]:
                document = references_document(kept, document["generated_at"])
                outcome = "pruned" if outcome != "written" else outcome
        if document is not None and document != existing:
            _write_compact(path, document)
        status = self.frozen.get("reference_status")
        latest = _load_reference_status(data / REFERENCE_STATUS_FILE)
        if status is not None:
            checkpoints = dict((latest or {}).get("checkpoints") or {})
            for key, checkpoint in status["checkpoints"].items():
                if (reference_time(checkpoint.get("attempted_at")) or MINIMUM_TIME) >= (
                        reference_time(checkpoints.get(key, {}).get("attempted_at")) or MINIMUM_TIME):
                    checkpoints[key] = checkpoint
            if links is not None:
                linked = {appid for appid in links if appid not in steam and appid not in blocked}
                checkpoints = {key: value for key, value in checkpoints.items() if key.isdigit() and int(key) in linked}
            write_json(data / REFERENCE_STATUS_FILE, {**status, "checkpoints": checkpoints,
                                                      "reference_count": len((document or {}).get("games", []))})
        return {row["appid"] for row in (document or {}).get("games", [])}, outcome

    def __call__(self, frontend: Path) -> None:
        data = frontend / "data"
        strict_catalog(data, require_index=True)
        blocked = catalog.excluded_public_appids(data)
        steam = {int(load_json(path)["appid"]) for path in (data / "games").glob("*.json")}
        references, reference_result = self._references(data, steam, blocked)
        # Steam versions of console games get media and store facts like catalog games.
        games = (steam | references) - blocked
        media = data / MEDIA_DIR
        updated, superseded = [], []
        for document in self.frozen["documents"]:
            appid = document["appid"]
            path = media / f"{appid}.json"
            # A media-only job never keeps media for a game that left the catalog.
            if appid not in games:
                superseded.append(appid)
                continue
            existing = load_json(path) if path.is_file() else None
            if (valid_document(existing, appid) and (aware_time(existing.get("updated_at")) or MINIMUM_TIME)
                    > aware_time(document["updated_at"])):
                superseded.append(appid)
                continue
            write_json(path, document)
            updated.append(appid)
        removed = []
        for path in sorted(media.glob("*.json")) if media.is_dir() else []:
            if not path.stem.isdigit() or int(path.stem) not in games:
                path.unlink()
                removed.append(int(path.stem) if path.stem.isdigit() else path.stem)
        # Store facts: a newer file on the latest data wins; games that left the
        # catalog (or were excluded) are dropped either way.
        facts_path, store = data / FACTS_FILE, self.frozen.get("store_facts")
        existing_facts = _load_facts(facts_path)
        facts_result = "unchanged"
        if store is not None and existing_facts is not None and (
                facts_time(existing_facts["checked_at"]) > facts_time(store["checked_at"])):
            facts_result, store = "superseded", None
        if store is None and existing_facts is not None and any(int(key) not in games for key in existing_facts["games"]):
            store, facts_result = existing_facts, "pruned"
        if store is not None:
            write_json(facts_path, {**store, "games": {key: value for key, value in store["games"].items()
                                                       if int(key) in games}})
            facts_result = "written" if facts_result == "unchanged" else facts_result
        latest = load_json(data / STATUS_FILE) if (data / STATUS_FILE).is_file() else {}
        checkpoints = dict(latest.get("checkpoints") or {})
        for key, checkpoint in self.frozen["status"]["checkpoints"].items():
            if (aware_time(checkpoint.get("attempted_at")) or MINIMUM_TIME) >= (
                    aware_time(checkpoints.get(key, {}).get("attempted_at")) or MINIMUM_TIME):
                checkpoints[key] = checkpoint
        checkpoints = {key: value for key, value in checkpoints.items() if key.isdigit() and int(key) in games}
        now = aware_time(self.frozen["prepared_at"])
        present = {int(path.stem) for path in media.glob("*.json")} if media.is_dir() else set()
        deadlines = {appid: due_at(checkpoints.get(str(appid), {}), appid in present) for appid in games}
        retry = retry_pending(games, checkpoints)
        pending = sorted({appid for appid, deadline in deadlines.items() if deadline <= now} | set(retry))
        result = {**{key: value for key, value in self.frozen["status"].items() if key != "checkpoints"},
                  "checkpoints": checkpoints, "game_count": len(games), "with_media": len(present),
                  "updated_appids": updated, "superseded_appids": superseded, "retry_pending_appids": retry,
                  "removed_appids": sorted(set(self.frozen["status"].get("removed_appids", [])) | set(removed),
                                           key=str),
                  "pending_count": len(pending), "complete": not pending and not superseded,
                  "store_facts": facts_result, "references": reference_result,
                  "reference_count": len(references - blocked),
                  "next_due_at": timestamp(max(min(deadlines.values()), now)) if deadlines else None}
        write_json(data / STATUS_FILE, result)
        write_json(data / MANIFEST, {"schema_version": 1, "operation": "steam_media",
                                    "input_revision": self.frozen["input_revision"],
                                    "payload_revision": self.payload_revision,
                                    "target_slot": self.frozen.get("target_slot"),
                                    "collection_complete": result["complete"]})
        self.result = {key: value for key, value in result.items() if key != "checkpoints"}
