"""Verify event identity and immutable Twitch admission before collection."""
from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import re
import urllib.request

from twitch_steam_admission import normalize_twitch_admission, validate_twitch_snapshot

FRONTEND = "danielet087/game-trend-radar"
EVIDENCE_PATHS = ("data/twitch_tracking.json", "data/twitch_steam_discovery.json")


def validate_event(environment, *, storage=None, opener=None):
    opener = opener or urllib.request.urlopen
    appid = environment.get("APPID", "")
    followers = environment.get("OFFICIAL_FOLLOWERS", "")
    release = environment.get("RELEASE_DATE", "")
    if not appid.isascii() or not appid.isdigit() or int(appid) <= 0:
        raise ValueError("Invalid Steam AppID")
    raw = json.loads(environment.get("TWITCH_ADMISSION_JSON") or "null")
    proof = normalize_twitch_admission(raw, appid)
    if raw is not None and proof is None:
        raise ValueError("Invalid Twitch admission proof")
    if environment.get("EVENT_ACTION") == "steam_game_twitch_discovered" and proof is None:
        raise ValueError("Missing Twitch admission proof")
    if not ((followers == "" and proof is not None) or (
        followers.isascii() and followers.isdigit() and (int(followers) >= 5000 or proof is not None)
    )):
        raise ValueError("Invalid official follower evidence")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release) or date.fromisoformat(release).isoformat() != release:
        raise ValueError("Invalid release date")
    if proof is not None:
        if environment.get("RADAR_DATABASE_PRIMARY") == "1":
            if storage is None:
                from radar_core.storage import primary as storage
            revision = proof.get("source_database_revision") or proof["source_frontend_commit"]
            head, files = storage.read_files(FRONTEND, EVIDENCE_PATHS, revision=revision)
            if proof.get("source_database_revision") and (
                head.get("manifest_digest") != proof["source_database_revision"]
                or head.get("generation") != proof["source_database_generation"]
            ):
                raise ValueError("Twitch database evidence revision or generation does not match")
            registry, discovery = (json.loads(files[path].decode("utf-8")) for path in EVIDENCE_PATHS)
        else:
            base = f"https://raw.githubusercontent.com/{FRONTEND}/{proof['source_frontend_commit']}/"
            with opener(base + EVIDENCE_PATHS[0], timeout=30) as response:
                registry = json.load(response)
            with opener(base + EVIDENCE_PATHS[1], timeout=30) as response:
                discovery = json.load(response)
        if not validate_twitch_snapshot(proof, registry, discovery):
            raise ValueError("Twitch admission does not match its immutable source evidence")
    return proof


def main():
    try:
        proof = validate_event(os.environ)
        if proof is not None:
            Path(os.environ["TWITCH_ADMISSION_PATH"]).write_text(json.dumps(proof), encoding="utf-8")
        print("CONTENT_EVENT_VALID", os.environ["APPID"], os.environ["OFFICIAL_FOLLOWERS"],
              os.environ["RELEASE_DATE"], "twitch_source", proof is not None)
    except Exception:
        raise SystemExit("Content event or its immutable source could not be verified.") from None


if __name__ == "__main__":
    main()
