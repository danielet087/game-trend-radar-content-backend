"""Frozen replay contracts exercised with local Git remotes and no source APIs."""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from radar_core.publication import PublicationError, SubprocessGitRepository, publish_with_retry, snapshot_revision
from radar_backend.publication import catalog
from radar_backend.publication.content_snapshot import (
    FrozenContentPublication, OWNED_PATHS, changed_paths, freeze_enrichment,
    freeze_reconciliation, strict_catalog, validate_snapshot,
)
from radar_backend.state.json_documents import load_json, write_json
from steam_player_modes import BROWSE_SOURCE

CHECKED = "2020-01-01T00:00:00Z"


def record(appid, **changes):
    return {"appid": appid, "name": f"Game {appid}", "followers": 6000,
            "follower_checked_at": CHECKED, "release_start": "2099-01-01",
            "release_precision": "day", "release_display_precision": "date_full",
            "release_date_timezone": "Asia/Taipei", "sexual_content_screened": True,
            "header_image": "verified.jpg", "language_support": {"english": True},
            "tags": [], "genres": [], "tags_fetch_status": "ok", "genres_fetch_status": "ok",
            "tag_labels_language": "zh-TW", "genre_labels_language": "zh-TW",
            "artwork_checked_at": CHECKED, "description_checked_at": CHECKED,
            "short_description_language": "zh-TW", "categories": [],
            "categories_source": BROWSE_SOURCE, "categories_checked_at": CHECKED,
            "content_enriched_at": CHECKED, **changes}


class LocalGitFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote, self.frontend, self.other = [self.root / name for name in ("remote.git", "frontend", "other")]
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "clone", str(self.remote), str(self.frontend))
        self.configure(self.frontend)
        write_json(self.frontend / "data/excluded_appids.json", {"appids": []})
        catalog.upsert_sharded(self.frontend / "data", record(101), "2099-01-01")
        self.commit(self.frontend, "Initial catalog")
        self.git(self.frontend, "push", "origin", "HEAD:main")
        self.git(self.root, "clone", str(self.remote), str(self.other))
        self.configure(self.other)

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                              capture_output=True).stdout.strip()

    def configure(self, cwd):
        self.git(cwd, "config", "user.name", "Tests")
        self.git(cwd, "config", "user.email", "tests@example.invalid")

    def commit(self, cwd, message):
        self.git(cwd, "add", "--all")
        self.git(cwd, "commit", "-m", message)

    def freeze(self, appid=123, **changes):
        catalog.upsert_sharded(self.frontend / "data", record(appid, **changes), "2099-01-01", force=True)
        return freeze_enrichment(self.frontend, appid=appid, followers=changes.get("followers", 6000),
                                 event_release_date="2099-01-01", force=False,
                                 input_revision=self.git(self.frontend, "rev-parse", "HEAD"))

    def publish(self, frozen, repository=None, attempts=12):
        replay = FrozenContentPublication(frozen)
        receipt = publish_with_retry(
            repository or SubprocessGitRepository(self.frontend, disposable_checkout=True), replay,
            paths=OWNED_PATHS, message="Frozen content", input_revision=frozen["input_revision"],
            payload_revision=replay.payload_revision, max_attempts=attempts,
        )
        return receipt, replay

    def remote_json(self, path):
        return json.loads(self.git(self.root, "--git-dir", str(self.remote), "show", f"main:{path}"))


class ContentPublicationTests(LocalGitFixture):
    def test_concurrent_appid_updates_replay_frozen_record_without_http(self):
        frozen = self.freeze()
        outer = self

        class ConcurrentRepository(SubprocessGitRepository):
            calls = 0

            def push(self, expected_revision=None):
                self.calls += 1
                if self.calls == 1:
                    catalog.upsert_sharded(outer.other / "data", record(456), "2099-01-01")
                    outer.commit(outer.other, "Competing game")
                    outer.git(outer.other, "push", "origin", "HEAD:main")
                return super().push(expected_revision)

        repository = ConcurrentRepository(self.frontend, disposable_checkout=True)
        with patch("requests.Session.get", side_effect=AssertionError("Publication made an HTTP source request")):
            receipt, replay = self.publish(frozen, repository)
        self.assertEqual(receipt.attempts, 2)
        self.assertEqual(receipt.published_revision, self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "main"))
        self.assertEqual({row["appid"] for row in self.remote_json("data/catalog.json")["games"]}, {101, 123, 456})
        self.assertEqual(self.remote_json("data/calendar/2099-01.json")["count"], 3)
        self.assertEqual(self.remote_json("data/publication/steam_content.json")["payload_revision"], snapshot_revision(frozen))
        self.assertNotEqual(receipt.target_snapshot_revision, replay.result["dataset_revision"])
        self.assertEqual(self.remote_json("data/index.json")["generated_at"], frozen["prepared_at"])
        validate_snapshot(self.frontend / "data")

    def test_noop_still_requires_acknowledged_remote_push(self):
        frozen = self.freeze()
        self.publish(frozen)

        class AcknowledgedRepository(SubprocessGitRepository):
            calls = 0
            def push(self, expected_revision=None):
                self.calls += 1
                return super().push(expected_revision)

        repository = AcknowledgedRepository(self.frontend, disposable_checkout=True)
        receipt, _ = self.publish(frozen, repository)
        self.assertFalse(receipt.changed)
        self.assertEqual(repository.calls, 1)

        class RejectedRepository(SubprocessGitRepository):
            def push(self, expected_revision=None):
                return False

        with self.assertRaises(PublicationError):
            self.publish(frozen, RejectedRepository(self.frontend, disposable_checkout=True), attempts=2)

    def test_newer_followers_or_date_cannot_be_claimed_as_old_payload(self):
        for changes in (
            {"followers": 7000, "follower_checked_at": "2021-01-01T00:00:00Z"},
            {"release_start": "2099-02-02", "release_date_verified_at": "2021-01-01T00:00:00Z", "follower_checked_at": "2022-01-01T00:00:00Z"},
        ):
            with self.subTest(changes=changes):
                self.git(self.frontend, "reset", "--hard", "origin/main")
                # Restore the other clone to the current remote before the next race.
                self.git(self.other, "fetch", "origin", "main")
                self.git(self.other, "reset", "--hard", "origin/main")
                frozen = self.freeze(follower_checked_at=None)
                catalog.upsert_sharded(self.other / "data", record(123, **changes), "2099-01-01", force=True)
                self.commit(self.other, "New official evidence")
                self.git(self.other, "push", "origin", "HEAD:main")
                previous = self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "main")
                with self.assertRaisesRegex(RuntimeError, "superseded"):
                    self.publish(frozen)
                self.assertEqual(self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "main"), previous)
                self.assertEqual(self.remote_json("data/games/123.json")["release_start"], changes.get("release_start", "2099-01-01"))

    def test_unknown_dirty_files_are_rejected_before_freezing(self):
        (self.frontend / "unexpected.txt").write_text("unrelated")
        with self.assertRaisesRegex(RuntimeError, "Unrelated dirty"):
            changed_paths(self.frontend)

    def test_partial_reconciliation_keeps_frozen_failures_and_does_not_fetch(self):
        self.freeze()
        master = self.root / "master.json"
        write_json(master, {"games": [record(101), record(123), record(456)]})
        status = {"generated_at": CHECKED, "qualified_master": 3, "attempted": 2, "enriched": 1,
                  "repaired_indexes": 0, "complete": False, "pending_count": 1,
                  "pending_enrichment": {"456": ["missing_record"]}, "public_count": 2,
                  "failures": {"456": {"attempted_at": CHECKED, "retry_after": "2020-01-01T06:00:00Z", "reason": "429"}}}
        write_json(self.frontend / "data/content_refresh_status.json", status)
        frozen = freeze_reconciliation(self.frontend, master_path=master,
                                       input_revision=self.git(self.frontend, "rev-parse", "HEAD"), source_revision="a" * 40)
        untouched = copy.deepcopy(frozen)
        with patch("requests.Session.get", side_effect=AssertionError("No source request in replay")):
            receipt, replay = self.publish(frozen)
        result = self.remote_json("data/content_refresh_status.json")
        self.assertFalse(result["complete"])
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["failures"], status["failures"])
        self.assertEqual(result["pending_enrichment"], {"456": ["missing_record"]})
        self.assertEqual(replay.result["pending_count"], 1)
        self.assertEqual(frozen, untouched)
        self.assertTrue(receipt.changed)
        self.publish(frozen)
        self.assertEqual(self.remote_json("data/content_refresh_status.json"), result)

    def test_reconciliation_marks_superseded_records_partial(self):
        self.freeze()
        master = self.root / "master.json"
        write_json(master, {"games": [record(101), record(123)]})
        status = {"complete": True, "failures": {}, "pending_enrichment": {},
                  "generated_at": CHECKED, "attempted": 1, "enriched": 1, "pending_count": 0}
        write_json(self.frontend / "data/content_refresh_status.json", status)
        frozen = freeze_reconciliation(self.frontend, master_path=master,
                                       input_revision=self.git(self.frontend, "rev-parse", "HEAD"), source_revision="a" * 40)
        catalog.upsert_sharded(self.other / "data", record(123, followers=7000,
                               follower_checked_at="2021-01-01T00:00:00Z"), "2099-01-01", force=True)
        self.commit(self.other, "Newer Followers")
        self.git(self.other, "push", "origin", "HEAD:main")
        _, replay = self.publish(frozen)
        self.assertFalse(replay.result["complete"])
        self.assertEqual(replay.result["superseded_appids"], [123])
        self.assertEqual(self.remote_json("data/games/123.json")["followers"], 7000)

    def test_newer_reconciliation_metadata_and_cooldown_survive_old_frozen_batch(self):
        self.freeze()
        master = self.root / "master.json"
        write_json(master, {"games": [record(101), record(123), record(456)]})
        old = {"complete": False, "failures": {}, "pending_enrichment": {"456": ["missing_record"]},
               "generated_at": CHECKED, "attempted": 1, "enriched": 1}
        write_json(self.frontend / "data/content_refresh_status.json", old)
        frozen = freeze_reconciliation(self.frontend, master_path=master,
                                       input_revision=self.git(self.frontend, "rev-parse", "HEAD"), source_revision="a" * 40)
        latest = {**old, "generated_at": "2021-01-01T00:00:00Z", "attempted": 7, "enriched": 6,
                  "failures": {"456": {"attempted_at": "2021-01-01T00:00:00Z",
                                       "retry_after": "2021-01-01T06:00:00Z", "reason": "newer 429"}}}
        write_json(self.other / "data/content_refresh_status.json", latest)
        self.commit(self.other, "Newer bounded reconciliation")
        self.git(self.other, "push", "origin", "HEAD:main")
        self.publish(frozen)
        result = self.remote_json("data/content_refresh_status.json")
        self.assertEqual(result["generated_at"], latest["generated_at"])
        self.assertEqual(result["attempted"], 7)
        self.assertEqual(result["enriched"], 6)
        self.assertEqual(result["failures"], latest["failures"])
        self.assertEqual(result["pending_enrichment"], {"456": ["missing_record"]})

    def test_partial_batch_does_not_become_complete_when_other_publisher_fills_gap(self):
        self.freeze()
        master = self.root / "master.json"
        write_json(master, {"games": [record(101), record(123), record(456)]})
        write_json(self.frontend / "data/content_refresh_status.json",
                   {"complete": False, "failures": {}, "pending_enrichment": {"456": ["missing_record"]},
                    "generated_at": CHECKED, "attempted": 1, "enriched": 1})
        frozen = freeze_reconciliation(self.frontend, master_path=master,
                                       input_revision=self.git(self.frontend, "rev-parse", "HEAD"), source_revision="a" * 40)
        catalog.upsert_sharded(self.other / "data", record(456), "2099-01-01")
        self.commit(self.other, "Other publisher supplies missing content")
        self.git(self.other, "push", "origin", "HEAD:main")
        _, replay = self.publish(frozen)
        self.assertTrue(replay.result["actual_catalog_complete"])
        self.assertEqual(replay.result["pending_count"], 0)
        self.assertFalse(replay.result["complete"])

    def test_retries_across_taipei_midnight_keep_frozen_clock_and_release_lists(self):
        before_midnight = datetime(2026, 10, 8, 15, 59, 59, tzinfo=timezone.utc)
        after_midnight = datetime(2026, 10, 8, 16, 0, 1, tzinfo=timezone.utc)
        incoming = record(123, release_start="2026-10-08")
        catalog.upsert_sharded(self.frontend / "data", incoming, incoming["release_start"],
                               force=True, generated_at=before_midnight.isoformat(), now=before_midnight)

        class PrepareClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return before_midnight.astimezone(tz) if tz else before_midnight.replace(tzinfo=None)

        with patch("radar_backend.publication.content_snapshot.datetime", PrepareClock):
            frozen = freeze_enrichment(self.frontend, appid=123, followers=6000,
                                       event_release_date=incoming["release_start"], force=False,
                                       input_revision=self.git(self.frontend, "rev-parse", "HEAD"))
        outer = self

        class MidnightClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return after_midnight.astimezone(tz) if tz else after_midnight.replace(tzinfo=None)

        class FirstPushRejectedRepository(SubprocessGitRepository):
            snapshots = []
            def push(self, expected_revision=None):
                self.snapshots.append(outer.git(self.root, "show", "HEAD:data/lists/upcoming.json"))
                if len(self.snapshots) == 1:
                    return False
                return super().push(expected_revision)

        repository = FirstPushRejectedRepository(self.frontend, disposable_checkout=True)
        with patch("radar_backend.publication.content_snapshot.datetime", MidnightClock), patch("radar_backend.publication.catalog.datetime", MidnightClock):
            receipt, _ = self.publish(frozen, repository)
        self.assertEqual(receipt.attempts, 2)
        self.assertEqual(repository.snapshots[0], repository.snapshots[1])
        self.assertIn(123, self.remote_json("data/lists/upcoming.json")["appids"])
        self.assertNotIn(123, self.remote_json("data/lists/released.json")["appids"])

    def test_cli_rejected_push_replaces_stale_success_receipt_with_failed_result(self):
        frozen = self.freeze()
        path, receipt_path = self.root / "frozen.json", self.root / "receipt.json"
        write_json(path, frozen)
        write_json(receipt_path, {"publication_receipt": {"published_revision": "fake-old-success"}})
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        package = Path(__file__).resolve().parent
        result = subprocess.run(
            [sys.executable, "-m", "radar_backend.jobs.publish_content", "publish",
             "--frontend", str(self.frontend), "--frozen", str(path), "--receipt", str(receipt_path)],
            cwd=package.parent, env=dict(os.environ, PYTHONPATH=str(package)),
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        receipt = load_json(receipt_path)
        self.assertNotIn("publication_receipt", receipt)
        self.assertEqual(receipt["job_result"]["status"], "failed")
        self.assertFalse(receipt["job_result"]["published"])
        self.assertEqual(self.remote_json("data/index.json")["game_count"], 1)

    def test_receipt_path_cannot_overwrite_frozen_input(self):
        path = self.root / "frozen.json"
        write_json(path, self.freeze())
        original = path.read_text()
        package = Path(__file__).resolve().parent
        result = subprocess.run(
            [sys.executable, "-m", "radar_backend.jobs.publish_content", "publish",
             "--frontend", str(self.frontend), "--frozen", str(path), "--receipt", str(path)],
            cwd=package.parent, env=dict(os.environ, PYTHONPATH=str(package)),
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(), original)

    def test_present_corrupt_catalog_documents_fail_closed(self):
        for name in ("games/101.json", "calendar/2099-01.json", "index.json", "catalog.json", "lists/upcoming.json"):
            with self.subTest(name=name):
                path = self.frontend / "data" / name
                old = path.read_text()
                path.write_text("{broken")
                with self.assertRaises(ValueError):
                    strict_catalog(self.frontend / "data")
                self.assertEqual(path.read_text(), "{broken")
                path.write_text(old)
        path = self.frontend / "data/index.json"
        path.write_text('{"game_count":1,"game_count":2,"months":[]}')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            strict_catalog(self.frontend / "data")

    def test_corrupt_authoritative_master_does_not_become_complete_empty_batch(self):
        master = self.root / "master.json"
        master.write_text("{broken")
        write_json(self.frontend / "data/content_refresh_status.json",
                   {"complete": False, "failures": {}, "pending_enrichment": {}})
        with self.assertRaises(ValueError):
            freeze_reconciliation(self.frontend, master_path=master,
                                  input_revision="b" * 40, source_revision="a" * 40)

    def test_projection_payload_is_checked_even_when_revision_header_matches(self):
        path = self.frontend / "data/catalog.json"
        value = load_json(path)
        value["games"][0]["name"] = "Corrupt projection retaining revision"
        write_json(path, value)
        with self.assertRaisesRegex(RuntimeError, "projection"):
            validate_snapshot(self.frontend / "data")

    def test_latest_adult_exclusion_stops_publishing_cached_record(self):
        frozen = self.freeze()
        write_json(self.other / "data/excluded_appids.json", {"appids": [123]})
        self.commit(self.other, "Adult audit")
        self.git(self.other, "push", "origin", "HEAD:main")
        with self.assertRaisesRegex(RuntimeError, "adult audit"):
            self.publish(frozen)
        self.assertFalse((self.other / "data/games/123.json").exists())


class WorkflowPublicationTests(unittest.TestCase):
    def test_publication_commands_use_write_auth_and_fixed_attempts_in_job(self):
        root = Path(__file__).resolve().parents[1]
        for workflow in ("steam-content-enrichment-dispatch.yml", "steam-catalog-reconcile.yml"):
            text = (root / ".github/workflows" / workflow).read_text()
            publish = text.split("      - name: Publish frozen", 1)[1].split("      - name:", 1)[0]
            self.assertIn("GH_TOKEN: ${{ secrets.FRONTEND_REPO_TOKEN }}", publish)
            self.assertIn("-m radar_backend.jobs.publish_content publish", publish)
            self.assertNotIn("for attempt", text)
            self.assertNotIn("git -C frontend reset", text)
        reconcile = (root / ".github/workflows/steam-catalog-reconcile.yml").read_text()
        self.assertEqual(reconcile.count("python -u content_backend/reconcile_catalog.py"), 1)
        summary = reconcile.split("      - name: Report acknowledged publication", 1)[1]
        self.assertIn('JobResult.from_dict(report["job_result"])', summary)
        self.assertIn('PublicationReceipt.from_dict(report["publication_receipt"])', summary)
        self.assertIn("job.successful", summary)
        self.assertIn("尚未收到本輪發布確認", summary)
        self.assertNotIn('Path("frontend/data/content_refresh_status.json")', summary)
        source = (root / "content_backend/radar_backend/jobs/publish_content.py").read_text()
        self.assertIn('max_attempts=12 if kind == "enrichment" else 8', source)


if __name__ == "__main__":
    unittest.main()
