"""Freeze description evidence and replay only those fields against latest data."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from radar_core.publication import snapshot_revision
from radar_backend.domain.description_refresh import aware_time, due_at, retry_pending, select_due, timestamp
from radar_backend.publication import catalog
from radar_backend.publication.content_snapshot import strict_catalog, validate_snapshot
from radar_backend.state.json_documents import load_json, write_json

STATUS = "description_refresh_status.json"
MANIFEST = "publication/steam_descriptions.json"
OWNED_PATHS = ("data/games/", "data/calendar/", "data/lists/", "data/index.json",
               "data/steam_upcoming.json", "data/catalog.json", "data/" + STATUS,
               "data/" + MANIFEST)
MINIMUM = datetime.min.replace(tzinfo=timezone.utc)


def description_key(key: str) -> bool:
    return key.startswith("short_description") or key.startswith("description_")


def freeze(frontend: Path, *, input_revision: str) -> dict:
    data = frontend / "data"
    strict_catalog(data, require_index=True)
    validate_snapshot(data)
    status = load_json(data / STATUS)
    if status.get("schema_version") != 1 or not isinstance(status.get("checkpoints"), dict):
        raise RuntimeError("Malformed description refresh status")
    patches = []
    for appid in status.get("updated_appids", []):
        record = load_json(data / "games" / f"{int(appid)}.json")
        fields = {key: value for key, value in record.items() if description_key(key)}
        if aware_time(fields.get("description_checked_at")) is None:
            raise RuntimeError("Description patch lacks successful check evidence")
        patches.append({"appid": int(appid), "fields": fields})
    return {"schema_version": 1, "kind": "steam_descriptions", "input_revision": input_revision,
            "prepared_at": timestamp(datetime.now(timezone.utc)), "patches": patches,
            "status": status}


class FrozenDescriptionPublication:
    def __init__(self, frozen: dict):
        if frozen.get("schema_version") != 1 or frozen.get("kind") != "steam_descriptions":
            raise RuntimeError("Malformed frozen description batch")
        if not isinstance(frozen.get("patches"), list) or aware_time(frozen.get("prepared_at")) is None:
            raise RuntimeError("Invalid frozen description evidence")
        for patch in frozen["patches"]:
            if (type(patch.get("appid")) is not int or not isinstance(patch.get("fields"), dict)
                    or any(not description_key(key) for key in patch["fields"])
                    or aware_time(patch["fields"].get("description_checked_at")) is None):
                raise RuntimeError("Frozen patch contains unrelated or unchecked fields")
        self.frozen = frozen
        self.payload_revision = snapshot_revision(frozen)
        self.result = {}

    def __call__(self, frontend: Path) -> None:
        data = frontend / "data"
        strict_catalog(data, require_index=True)
        now = aware_time(self.frozen["prepared_at"])
        superseded, updated = [], []
        blocked = catalog.excluded_public_appids(data)
        for patch in self.frozen["patches"]:
            appid, fields = patch["appid"], patch["fields"]
            path = data / "games" / f"{appid}.json"
            # A description-only job may never resurrect a removed/excluded game.
            if appid in blocked or not path.is_file():
                superseded.append(appid)
                continue
            existing = load_json(path)
            if (aware_time(existing.get("description_checked_at")) or MINIMUM) > aware_time(fields["description_checked_at"]):
                superseded.append(appid)
                continue
            merged = {key: value for key, value in existing.items() if not description_key(key)}
            merged.update(fields)
            catalog.upsert_sharded(data, merged, existing["release_start"], force=True,
                                   generated_at=self.frozen["prepared_at"], now=now)
            updated.append(appid)
        # Rebuild once even for an empty due queue, keeping today's list bounds.
        catalog._rebuild_small_indexes(data, generated_at=self.frozen["prepared_at"], now=now)
        latest = load_json(data / STATUS) if (data / STATUS).is_file() else {}
        checkpoints = dict(latest.get("checkpoints") or {})
        for key, checkpoint in self.frozen["status"]["checkpoints"].items():
            if (aware_time(checkpoint.get("attempted_at")) or MINIMUM) >= (aware_time(checkpoints.get(key, {}).get("attempted_at")) or MINIMUM):
                checkpoints[key] = checkpoint
        records = [load_json(path) for path in (data / "games").glob("*.json")]
        existing_appids = {str(row["appid"]) for row in records}
        checkpoints = {key: {name: item for name, item in value.items() if name not in {"translation_pending", "translation_retry_at"}}
                       for key, value in checkpoints.items() if key in existing_appids}
        remaining = select_due(records, checkpoints, now, len(records))
        deadlines = [due_at(row, checkpoints.get(str(row["appid"]), {}), now) for row in records]
        failed = retry_pending(records, checkpoints)
        pending_appids = {int(row["appid"]) for row in remaining} | set(failed)
        result = {**self.frozen["status"], "checkpoints": checkpoints, "pending_count": len(pending_appids),
                  "retry_pending_appids": failed,
                  "complete": not pending_appids and not superseded,
                  "updated_appids": updated, "superseded_appids": superseded,
                  "next_due_at": timestamp(min(deadlines)) if deadlines else None}
        write_json(data / STATUS, result)
        validate_snapshot(data, now=now)
        write_json(data / MANIFEST, {"schema_version": 1, "operation": "steam_descriptions",
                                    "input_revision": self.frozen["input_revision"],
                                    "payload_revision": self.payload_revision,
                                    "target_slot": self.frozen.get("target_slot"),
                                    "collection_complete": result["complete"]})
        self.result = {key: value for key, value in result.items() if key != "checkpoints"}
