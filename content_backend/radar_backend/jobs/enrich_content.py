"""CLI parsing and validation for the content batch use case."""
from __future__ import annotations
import argparse
import json
import logging
from pathlib import Path
from radar_core.domain.twitch_admission import normalize_twitch_admission
from radar_backend.domain.content import valid_date, parse_followers, validate_enrichment_input
from radar_backend.publication.catalog import excluded_public_appids, upsert_sharded
from radar_backend.bootstrap import build_record as production_build_record, content_session
from radar_backend.state.json_documents import load_json

def main(*, build_record=production_build_record, publish=upsert_sharded) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--appid", type=int, required=True)
    parser.add_argument("--followers", type=parse_followers, required=True)
    parser.add_argument("--release-date", required=True)
    parser.add_argument("--follower-checked-at")
    parser.add_argument("--follower-status")
    parser.add_argument("--follower-unavailable-at")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument('--twitch-admission', type=Path, help='Verified Twitch discovery proof JSON')
    args = parser.parse_args()
    if args.appid <= 0:
        raise SystemExit("appid must be positive")
    admission = None
    if args.twitch_admission is not None:
        admission = normalize_twitch_admission(load_json(args.twitch_admission), args.appid)
        if admission is None:
            raise SystemExit('Invalid Twitch Steam admission proof')
    try:
        validate_enrichment_input(args.appid, args.followers, admission,
                                 follower_checked_at=args.follower_checked_at or None,
                                 follower_status=args.follower_status,
                                 follower_unavailable_at=args.follower_unavailable_at)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    if not valid_date(args.release_date):
        raise SystemExit("release-date must be an exact YYYY-MM-DD")
    if not (args.data_dir / "index.json").exists():
        raise SystemExit(f"Missing sharded frontend index: {args.data_dir / 'index.json'}")

    if args.appid in excluded_public_appids(args.data_dir):
        raise SystemExit(f"AppID {args.appid} excluded by official Steam adult-content audit")

    session = content_session()
    availability = ({"follower_status": args.follower_status,
                     "follower_unavailable_at": args.follower_unavailable_at}
                    if args.followers is None else {})
    record = build_record(
        session,
        appid=args.appid,
        followers=args.followers,
        event_release_date=args.release_date,
        follower_checked_at=args.follower_checked_at or None,
        twitch_admission=admission,
        **availability,
    )
    changed = publish(
        args.data_dir, record, args.release_date, force=args.force
    )
    print(
        "CONTENT_ENRICHMENT_RESULT",
        json.dumps({
            "appid": args.appid,
            "followers": args.followers,
            "release_date": record["release_start"],
            "tags": len(record["tags"]),
            "genres": len(record["genres"]),
            "header": bool(record["header_image"]),
            "main_capsule": bool(record["main_capsule_image"]),
            "changed": changed,
            "force": args.force,
        }, ensure_ascii=False),
        flush=True,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
