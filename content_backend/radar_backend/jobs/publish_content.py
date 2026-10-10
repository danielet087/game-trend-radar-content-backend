"""Freeze once, then replay local JSON through the shared Git publication port."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess

from radar_core.jobs import JobResult, JobStatus
from radar_core.publication import SubprocessGitRepository, publish_with_retry
from radar_backend.publication.content_snapshot import (
    FrozenContentPublication, OWNED_PATHS, freeze_enrichment, freeze_reconciliation,
    load_json, strict_catalog,
)
from radar_backend.state.json_documents import write_json
from radar_backend.domain.content import parse_followers


def _revision(frontend: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=frontend, check=True,
                          capture_output=True, text=True).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="Reject corrupt catalog before collection writes")
    validate.add_argument("--frontend", type=Path, required=True)
    validate.add_argument("--master", type=Path)
    enrich = sub.add_parser("freeze-enrichment")
    enrich.add_argument("--appid", type=int, required=True)
    enrich.add_argument("--followers", type=parse_followers, required=True)
    enrich.add_argument("--release-date", required=True)
    enrich.add_argument("--force", action="store_true")
    reconcile = sub.add_parser("freeze-reconciliation")
    reconcile.add_argument("--master", type=Path, required=True)
    reconcile.add_argument("--source-revision", required=True)
    for command in (enrich, reconcile):
        command.add_argument("--frontend", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    publish = sub.add_parser("publish")
    publish.add_argument("--frontend", type=Path, required=True)
    publish.add_argument("--frozen", type=Path, required=True)
    publish.add_argument("--receipt", type=Path, required=True)
    publish.add_argument("--target-slot")
    args = parser.parse_args()
    if args.command == "validate":
        strict_catalog(args.frontend / "data", require_index=True)
        if args.master:
            value = load_json(args.master)
            if not isinstance(value, dict) or not isinstance(value.get("games"), list):
                raise SystemExit("Malformed authoritative master")
        return
    if args.command == "freeze-enrichment":
        frozen = freeze_enrichment(args.frontend, appid=args.appid, followers=args.followers,
                                   event_release_date=args.release_date, force=args.force,
                                   input_revision=_revision(args.frontend))
        write_json(args.output, frozen)
        return
    if args.command == "freeze-reconciliation":
        frozen = freeze_reconciliation(args.frontend, master_path=args.master,
                                       input_revision=_revision(args.frontend), source_revision=args.source_revision)
        write_json(args.output, frozen)
        return
    frontend_root = args.frontend.resolve()
    receipt_path, frozen_path = args.receipt.resolve(), args.frozen.resolve()
    if receipt_path == frozen_path:
        raise SystemExit("Publication receipt must not overwrite the frozen batch")
    if frontend_root in receipt_path.parents or frontend_root in frozen_path.parents:
        raise SystemExit("Frozen inputs and receipt must remain outside the disposable frontend checkout")
    args.receipt.unlink(missing_ok=True)
    frozen = load_json(args.frozen)
    replay = FrozenContentPublication(frozen)
    repository = SubprocessGitRepository(args.frontend, disposable_checkout=True)
    kind = frozen["kind"]
    try:
        receipt = publish_with_retry(
            repository, replay, paths=OWNED_PATHS,
            message=(f"data: enrich Steam AppID shard {frozen['records'][0]['appid']} [skip ci]"
                     if kind == "enrichment" else "data: reconcile qualified Steam catalog [skip ci]"),
            input_revision=frozen["input_revision"], payload_revision=replay.payload_revision,
            max_attempts=12 if kind == "enrichment" else 8,
        )
    except Exception:
        failure = JobResult(job="steam_content_enrichment" if kind == "enrichment" else "steam_catalog_reconcile",
                            status=JobStatus.FAILED, reason="Content publication was not acknowledged",
                            requires_publication=True, input_revision=frozen["input_revision"],
                            target_slot=args.target_slot or None)
        write_json(args.receipt, {"job_result": failure.to_dict(), "content_result": replay.result})
        raise
    complete = replay.result["complete"]
    job = JobResult(job="steam_content_enrichment" if kind == "enrichment" else "steam_catalog_reconcile",
                    status=JobStatus.COMPLETE if complete else JobStatus.PARTIAL,
                    reason="" if complete else "Published valid records; the collected batch remains partial or superseded",
                    collection_complete=complete, state_persisted=True, published=True,
                    requires_publication=True, input_revision=frozen["input_revision"],
                    target_slot=args.target_slot or None)
    report = {"publication_receipt": receipt.to_dict(), "job_result": job.to_dict(),
              "content_result": replay.result}
    write_json(args.receipt, report)
    print("CONTENT_PUBLICATION_RESULT", json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
