"""Hydrate disposable collection inputs from the primary database snapshot."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

FRONTEND = "danielet087/game-trend-radar"
BACKEND = "danielet087/game-trend-radar-backend"
MASTER = "data/steam_upcoming_master.json"


def prepare(frontend, *, backend=None, storage=None):
    if os.environ.get("RADAR_DATABASE_PRIMARY") != "1":
        result = {}
        if backend is not None:
            result["master_revision"] = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=backend, check=True,
                capture_output=True, text=True).stdout.strip()
        return result
    if storage is None:
        from radar_core.storage import primary as storage
    head = storage.hydrate(frontend, FRONTEND, clean_baseline=True)
    result = {"frontend_revision": head["revision"], "frontend_generation": head["generation"]}
    if backend is not None:
        master_head, files = storage.read_files(BACKEND, [MASTER])
        raw = files[MASTER]
        document = json.loads(raw.decode("utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("games"), list):
            raise ValueError("The primary Steam master is malformed")
        path = Path(backend) / MASTER
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_bytes(raw)
        temporary.replace(path)
        result["master_revision"] = master_head["revision"]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--backend", type=Path)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.frontend, backend=args.backend)
        if output := os.environ.get("GITHUB_OUTPUT"):
            with open(output, "a", encoding="utf-8") as handle:
                for key, value in result.items():
                    handle.write(f"{key}={value}\n")
        if result.get("frontend_revision") and (environment := os.environ.get("GITHUB_ENV")):
            with open(environment, "a", encoding="utf-8") as handle:
                handle.write("RADAR_INPUT_REVISION=" + result["frontend_revision"] + "\n")
        print(json.dumps({"status": "ready", **result}))
    except Exception:
        raise SystemExit("Primary collection inputs could not be verified; no Git fallback was used.") from None


if __name__ == "__main__":
    main()
