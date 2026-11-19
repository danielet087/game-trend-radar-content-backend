"""Publish a frozen media-only batch with the shared cross-repo retry port."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from radar_core.jobs import JobResult, JobStatus
from radar_core.publication import SubprocessGitRepository, publish_with_retry
from radar_backend.publication.media_snapshot import FrozenMediaPublication, OWNED_PATHS, freeze
from radar_backend.state.json_documents import load_json, write_json
from radar_backend.jobs.publish_content import _revision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("freeze")
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--target-slot")
    publish = sub.add_parser("publish")
    publish.add_argument("--frozen", type=Path, required=True)
    publish.add_argument("--receipt", type=Path, required=True)
    publish.add_argument("--target-slot")
    for command in (prepare, publish):
        command.add_argument("--frontend", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        frozen = freeze(args.frontend, input_revision=_revision(args.frontend))
        frozen["target_slot"] = args.target_slot or None
        write_json(args.output, frozen)
        return
    frontend = args.frontend.resolve()
    if args.receipt.resolve() == args.frozen.resolve() or any(frontend in path.resolve().parents for path in (args.receipt, args.frozen)):
        raise SystemExit("Frozen input and receipt must remain outside disposable checkout")
    args.receipt.unlink(missing_ok=True)
    frozen = load_json(args.frozen)
    replay = FrozenMediaPublication(frozen)
    status = frozen["status"]
    if (not frozen["documents"] and not status.get("removed_appids") and not status.get("due_count")
            and frozen.get("store_facts") is None):
        # Nothing was checked or removed: publishing would only move timestamps.
        job = JobResult(job="steam_media_refresh", status=JobStatus.SKIPPED, reason="No media was due and store facts are unchanged",
                        input_revision=frozen["input_revision"], target_slot=args.target_slot or None)
        write_json(args.receipt, {"job_result": job.to_dict(), "media_result": {
            key: value for key, value in status.items() if key != "checkpoints"}})
        print("MEDIA_PUBLICATION_SKIPPED", json.dumps({"next_due_at": status.get("next_due_at")}), flush=True)
        return
    try:
        receipt = publish_with_retry(SubprocessGitRepository(args.frontend, disposable_checkout=True), replay,
                                     paths=OWNED_PATHS, message="data: refresh Steam media [skip ci]",
                                     input_revision=frozen["input_revision"], payload_revision=replay.payload_revision,
                                     max_attempts=8)
    except Exception:
        job = JobResult(job="steam_media_refresh", status=JobStatus.FAILED,
                        reason="Media publication was not acknowledged", requires_publication=True,
                        input_revision=frozen["input_revision"], target_slot=args.target_slot or None)
        write_json(args.receipt, {"job_result": job.to_dict(), "media_result": replay.result})
        raise
    complete = replay.result["complete"]
    job = JobResult(job="steam_media_refresh", status=JobStatus.COMPLETE if complete else JobStatus.PARTIAL,
                    reason="" if complete else "Published refreshed media and retry checkpoint; queue remains",
                    collection_complete=complete, state_persisted=True, published=True, requires_publication=True,
                    input_revision=frozen["input_revision"], target_slot=args.target_slot or None)
    report = {"publication_receipt": receipt.to_dict(), "job_result": job.to_dict(), "media_result": replay.result}
    write_json(args.receipt, report)
    print("MEDIA_PUBLICATION_RESULT", json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
