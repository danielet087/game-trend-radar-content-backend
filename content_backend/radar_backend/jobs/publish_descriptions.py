"""Publish a frozen description-only batch with the shared cross-repo retry port."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from radar_core.jobs import JobResult, JobStatus
from radar_core.publication import SubprocessGitRepository, publish_with_retry
from radar_backend.publication.description_snapshot import FrozenDescriptionPublication, OWNED_PATHS, freeze
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
        revision = _revision(args.frontend)
        frozen = freeze(args.frontend, input_revision=revision)
        frozen["target_slot"] = args.target_slot or None
        write_json(args.output, frozen)
        return
    frontend = args.frontend.resolve()
    if args.receipt.resolve() == args.frozen.resolve() or any(frontend in path.resolve().parents for path in (args.receipt, args.frozen)):
        raise SystemExit("Frozen input and receipt must remain outside disposable checkout")
    args.receipt.unlink(missing_ok=True)
    frozen = load_json(args.frozen)
    replay = FrozenDescriptionPublication(frozen)
    try:
        receipt = publish_with_retry(SubprocessGitRepository(args.frontend, disposable_checkout=True), replay,
                                     paths=OWNED_PATHS, message="data: refresh localized Steam descriptions [skip ci]",
                                     input_revision=frozen["input_revision"], payload_revision=replay.payload_revision,
                                     max_attempts=8)
    except Exception:
        job = JobResult(job="steam_description_refresh", status=JobStatus.FAILED,
                        reason="Description publication was not acknowledged", requires_publication=True,
                        input_revision=frozen["input_revision"], target_slot=args.target_slot or None)
        write_json(args.receipt, {"job_result": job.to_dict(), "description_result": replay.result})
        raise
    complete = replay.result["complete"]
    job = JobResult(job="steam_description_refresh", status=JobStatus.COMPLETE if complete else JobStatus.PARTIAL,
                    reason="" if complete else "Published successful descriptions and retry checkpoint; queue remains",
                    collection_complete=complete, state_persisted=True, published=True, requires_publication=True,
                    input_revision=frozen["input_revision"], target_slot=args.target_slot or None)
    report = {"publication_receipt": receipt.to_dict(), "job_result": job.to_dict(), "description_result": replay.result}
    write_json(args.receipt, report)
    print("DESCRIPTION_PUBLICATION_RESULT", json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
