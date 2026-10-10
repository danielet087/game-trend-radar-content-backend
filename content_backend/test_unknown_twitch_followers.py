"""A missing GroupID keeps genuine null Followers through content publication."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from enrich_game import build_record, upsert_document, upsert_sharded, _shard_in_sync
from public_catalog import keep_newer_release
from reconcile_catalog import metadata_gaps, reconcile
from radar_backend.domain.catalog import enrichment_signature
from radar_backend.jobs.enrich_content import main as enrich_cli
from radar_backend.jobs.publish_content import main as publish_cli
from radar_backend.publication.content_snapshot import (
    freeze_enrichment, freeze_reconciliation, strict_catalog, validate_snapshot,
)
from radar_backend.state.json_documents import write_json
from radar_core.domain.twitch_admission import has_unavailable_group_followers, is_twitch_qualified
from test_content_publication import LocalGitFixture, record
import test_twitch_steam_admission as admission_fixture
from test_twitch_steam_admission import build, mocks, proof

STAMP = "2026-10-02T09:00:00Z"


def unknown(day="2030-01-01"):
    return build(base=mocks(day), followers=None, event_release_date=day,
                 follower_checked_at=None, follower_status="unavailable_group_id",
                 follower_unavailable_at=STAMP)


class UnknownEnrichmentTests(unittest.TestCase):
    def test_null_is_explicit_and_real_zero_remains_a_measurement(self):
        unavailable = unknown()
        for field in ("followers", "follower_checked_at", "follower_source", "group_id64"):
            self.assertIsNone(unavailable[field], field)
        self.assertFalse(unavailable["official_ge5000"])
        self.assertEqual(unavailable["follower_status"], "unavailable_group_id")
        self.assertEqual(unavailable["follower_unavailable_at"], STAMP)
        self.assertTrue(has_unavailable_group_followers(unavailable))
        self.assertTrue(is_twitch_qualified(unavailable))
        measured = build(followers=0)
        self.assertEqual(measured["followers"], 0)
        self.assertIsNotNone(measured["follower_checked_at"])
        self.assertEqual(measured["follower_source"], "Steam Community XML memberCount")
        self.assertNotIn("follower_status", measured)
        self.assertNotIn("follower_unavailable_at", measured)

    def test_unknown_needs_missing_group_evidence_before_any_source_call(self):
        for changes in (
            {"twitch_admission": None}, {"twitch_admission": {"source": "twitch"}},
            {"follower_status": None}, {"follower_status": "rate_limited"},
            {"follower_unavailable_at": None}, {"follower_unavailable_at": "2026-10-02"},
            {"follower_unavailable_at": "2026-10-02T07:59:59Z"},
            {"follower_checked_at": STAMP},
        ):
            args = dict(appid=123, followers=None, event_release_date="2030-01-01",
                        follower_checked_at=None, twitch_admission=proof(),
                        follower_status="unavailable_group_id", follower_unavailable_at=STAMP)
            args.update(changes)
            with self.subTest(changes=changes), patch("enrich_game.browse_one") as browse:
                with self.assertRaises(RuntimeError):
                    build_record(object(), **args)
                browse.assert_not_called()

    def test_unknown_does_not_bypass_game_identity_adult_or_exact_date(self):
        for base, details in (
            (mocks(), {"type": "dlc", "steam_appid": 123}),
            (mocks(), {"type": "game", "steam_appid": 999}),
            (mocks(content_descriptorids=[3]), {"type": "game", "steam_appid": 123}),
            (mocks(release={"coming_soon_display": "month"}), {"type": "game", "steam_appid": 123}),
        ):
            with self.subTest(base=base, details=details), self.assertRaises(RuntimeError):
                build(base=base, details=details, followers=None, follower_checked_at=None,
                      follower_status="unavailable_group_id", follower_unavailable_at=STAMP)

    def test_cli_requires_literal_null_with_verified_proof_and_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            write_json(data / "index.json", {"game_count": 0})
            write_json(data / "excluded_appids.json", {"appids": []})
            admission_path = data / "proof.json"
            write_json(admission_path, proof())
            args = ["enrich", "--appid", "123", "--followers", "null", "--release-date", "2030-01-01",
                    "--data-dir", str(data), "--twitch-admission", str(admission_path),
                    "--follower-status", "unavailable_group_id", "--follower-unavailable-at", STAMP]
            make, publish = Mock(return_value=unknown()), Mock(return_value=True)
            with patch("sys.argv", args), patch("radar_backend.jobs.enrich_content.content_session", return_value=object()):
                enrich_cli(build_record=make, publish=publish)
            self.assertIsNone(make.call_args.kwargs["followers"])
            self.assertIsNone(make.call_args.kwargs["follower_checked_at"])
            self.assertEqual(make.call_args.kwargs["follower_unavailable_at"], STAMP)
            for altered in (args[:-2], args[:args.index("--twitch-admission")] + args[args.index("--follower-status"):]):
                make.reset_mock()
                with patch("sys.argv", altered), self.assertRaises(SystemExit):
                    enrich_cli(build_record=make, publish=publish)
                make.assert_not_called()


class UnknownCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        write_json(self.data / "excluded_appids.json", {"appids": []})

    def test_null_and_status_reach_detail_calendar_browser_and_lists(self):
        source = unknown()
        self.assertTrue(upsert_sharded(self.data, source, source["release_start"]))
        validate_snapshot(self.data)
        detail = json.loads((self.data / "games/123.json").read_text())
        self.assertTrue(_shard_in_sync(self.data, detail))
        for filename in ("games/123.json", "calendar/2030-01.json", "catalog.json", "steam_upcoming.json"):
            document = json.loads((self.data / filename).read_text())
            actual = document["games"][0] if "games" in document else document
            self.assertIsNone(actual["followers"], filename)
            self.assertIsNone(actual["follower_checked_at"], filename)
            self.assertIsNone(actual["follower_source"], filename)
            self.assertIsNone(actual["group_id64"], filename)
            self.assertFalse(actual["official_ge5000"], filename)
            self.assertEqual(actual["follower_status"], "unavailable_group_id", filename)
            self.assertEqual(actual["follower_unavailable_at"], STAMP, filename)
            self.assertTrue(is_twitch_qualified(actual), filename)
        self.assertEqual(json.loads((self.data / "lists/upcoming.json").read_text())["appids"], [123])
        self.assertEqual(json.loads((self.data / "index.json").read_text())["game_count"], 1)
        self.assertFalse(upsert_sharded(self.data, source, source["release_start"]))

    def test_malformed_null_cannot_write_or_pass_strict_validation(self):
        source = unknown()
        for changes in ({"twitch_admission": None}, {"follower_status": None},
                        {"follower_source": "Steam XML"}, {"follower_checked_at": STAMP},
                        {"official_ge5000": True}, {"group_id64": "103582791429521412"},
                        {"sexual_content_screened": False}, {"steam_type": "dlc"}):
            bad = dict(source, **changes)
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(RuntimeError, "invalid Twitch-source"):
                    upsert_sharded(self.data, bad, bad["release_start"])
                self.assertFalse((self.data / "games/123.json").exists())
                write_json(self.data / "games/123.json", bad)
                with self.assertRaisesRegex(RuntimeError, "Malformed AppID source"):
                    strict_catalog(self.data)
                (self.data / "games/123.json").unlink()

    def test_projection_repair_retains_null_without_fetching_followers(self):
        source = unknown()
        upsert_sharded(self.data, source, source["release_start"])
        projection = json.loads((self.data / "catalog.json").read_text())
        projection["games"][0]["followers"] = 0
        write_json(self.data / "catalog.json", projection)
        with patch("requests.Session.get", side_effect=AssertionError("Offline projection repair")):
            self.assertTrue(upsert_sharded(self.data, source, source["release_start"]))
        self.assertIsNone(json.loads((self.data / "catalog.json").read_text())["games"][0]["followers"])
        validate_snapshot(self.data)

    def test_known_count_cannot_be_erased_and_recovered_count_clears_unavailable_markers(self):
        missing = unknown()
        upsert_sharded(self.data, missing, missing["release_start"])
        known = {**missing, "followers": 6200, "follower_checked_at": "2026-10-03T00:00:00Z",
                 "follower_source": "Steam Community XML memberCount", "official_ge5000": True,
                 "group_id64": "103582791429521412"}
        upsert_sharded(self.data, known, known["release_start"], force=True)
        upgraded = json.loads((self.data / "games/123.json").read_text())
        self.assertEqual(upgraded["followers"], 6200)
        self.assertNotIn("follower_status", upgraded)
        self.assertNotIn("follower_unavailable_at", upgraded)
        self.assertTrue(is_twitch_qualified(upgraded))
        later_failure = {**missing, "follower_unavailable_at": "2026-10-04T00:00:00Z", "tags": ["Adventure"]}
        upsert_sharded(self.data, later_failure, later_failure["release_start"], force=True)
        preserved = json.loads((self.data / "games/123.json").read_text())
        for field in ("followers", "follower_checked_at", "follower_source", "official_ge5000", "group_id64"):
            self.assertEqual(preserved[field], upgraded[field], field)
        self.assertEqual(preserved["tags"], ["Adventure"])
        self.assertNotIn("follower_status", preserved)
        self.assertNotIn("follower_unavailable_at", preserved)
        validate_snapshot(self.data)

    def test_metadata_only_updates_keep_unknown_but_explicit_invalid_null_does_not(self):
        source = unknown()
        keep = keep_newer_release(source, {"appid": 123, "name": "Translated title"})
        self.assertIsNone(keep["followers"])
        self.assertEqual(keep["follower_unavailable_at"], STAMP)
        explicit = keep_newer_release(source, {"appid": 123, "followers": None})
        self.assertNotIn("follower_status", explicit)
        self.assertFalse(has_unavailable_group_followers(explicit))

    def test_zero_unknown_and_fresh_availability_are_different_cache_inputs(self):
        source = unknown()
        measured = {**source, "followers": 0, "follower_checked_at": STAMP}
        measured.pop("follower_status")
        measured.pop("follower_unavailable_at")
        self.assertNotEqual(enrichment_signature(123, source), enrichment_signature(123, measured))
        self.assertNotEqual(enrichment_signature(123, source), enrichment_signature(123, {**source, "follower_unavailable_at": "2026-10-03T00:00:00Z"}))
        self.assertEqual(metadata_gaps(source, source), [])
        self.assertIn("source_changed", metadata_gaps(source, measured))

    def test_reconciliation_accepts_null_passes_evidence_and_rejects_bare_null(self):
        source = unknown()
        master = self.data.parent / "master.json"
        write_json(master, {"games": [source, {**source, "appid": 999, "twitch_admission": None}]})
        with patch("reconcile_catalog.build_record", return_value=source) as make:
            status = reconcile(master, self.data, 10)
        self.assertEqual(status["qualified_master"], 1)
        self.assertEqual(status["public_count"], 1)
        self.assertTrue(status["complete"])
        self.assertIsNone(make.call_args.kwargs["followers"])
        self.assertEqual(make.call_args.kwargs["follower_unavailable_at"], STAMP)
        self.assertEqual(make.call_args.kwargs["twitch_admission"], proof())
        validate_snapshot(self.data)

    def test_legacy_document_publication_preserves_null_and_does_not_repeat_enrichment(self):
        path = self.data / "legacy.json"
        write_json(path, {"games": []})
        source = unknown()
        self.assertTrue(upsert_document(path, source, source["release_start"]))
        self.assertFalse(upsert_document(path, source, source["release_start"]))
        self.assertIsNone(json.loads(path.read_text())["games"][0]["followers"])

    def test_old_master_does_not_recreate_gaps_for_newer_missing_group_observation(self):
        old = unknown()
        new = {**old, "follower_unavailable_at": "2026-10-04T00:00:00Z"}
        upsert_sharded(self.data, new, new["release_start"])
        master = self.data.parent / "master.json"
        write_json(master, {"games": [old]})
        with patch("reconcile_catalog.build_record", side_effect=AssertionError("Already current")) as make:
            status = reconcile(master, self.data, 10)
        make.assert_not_called()
        self.assertTrue(status["complete"])
        self.assertEqual(status["attempted"], 0)
        self.assertEqual(json.loads((self.data / "games/123.json").read_text())["follower_unavailable_at"], new["follower_unavailable_at"])

    def test_measured_zero_sorts_before_unknown_on_the_same_day(self):
        source = unknown()
        measured = build(appid=999, followers=0, twitch_admission=proof(999),
                         base=mocks(appid=999, assets={"asset_url_format": "steam/apps/999/${FILENAME}", "header": "header.jpg"}),
                         details={"type": "game", "steam_appid": 999})
        upsert_sharded(self.data, source, source["release_start"])
        upsert_sharded(self.data, measured, measured["release_start"])
        validate_snapshot(self.data)
        self.assertEqual([row["appid"] for row in json.loads((self.data / "catalog.json").read_text())["games"]], [999, 123])


class UnknownPublicationTests(LocalGitFixture):
    def freeze_unknown(self):
        source = unknown("2099-01-01")
        catalog_dir = self.frontend / "data"
        upsert_sharded(catalog_dir, source, source["release_start"], force=True)
        return freeze_enrichment(self.frontend, appid=123, followers=None,
                                 event_release_date=source["release_start"], force=False,
                                 input_revision=self.git(self.frontend, "rev-parse", "HEAD"))

    def test_freeze_and_acknowledged_git_publication_keep_real_null_everywhere(self):
        frozen = self.freeze_unknown()
        self.assertIsNone(frozen["records"][0]["followers"])
        with patch("requests.Session.get", side_effect=AssertionError("Frozen replay cannot collect sources")):
            receipt, replay = self.publish(frozen)
        self.assertTrue(receipt.changed)
        self.assertTrue(replay.result["complete"])
        for name in ("data/games/123.json", "data/catalog.json", "data/calendar/2099-01.json"):
            result = self.remote_json(name)
            value = next(row for row in result["games"] if row["appid"] == 123) if "games" in result else result
            self.assertIsNone(value["followers"])
            self.assertTrue(is_twitch_qualified(value))
        self.assertEqual(self.remote_json("data/lists/upcoming.json")["appids"], [101, 123])
        repeat, _ = self.publish(frozen)
        self.assertFalse(repeat.changed)

    def test_freeze_cli_carries_json_null_and_no_zero_substitution(self):
        frozen = self.freeze_unknown()
        output = self.root / "frozen.json"
        with patch("sys.argv", ["publish", "freeze-enrichment", "--frontend", str(self.frontend),
                                "--appid", "123", "--followers", "null", "--release-date", "2099-01-01",
                                "--output", str(output)]):
            publish_cli()
        result = json.loads(output.read_text())
        self.assertIsNone(result["records"][0]["followers"])
        self.assertEqual(result["records"], frozen["records"])

    def test_unknown_dispatch_upsert_freeze_and_publish_preserve_existing_real_count(self):
        for count in (0, 240, 6200):
            with self.subTest(count=count):
                source = unknown("2099-01-01")
                known = {**source, "followers": count, "follower_checked_at": "2026-10-03T00:00:00Z",
                         "follower_source": "Steam Community XML memberCount", "official_ge5000": count >= 5000}
                known.pop("follower_status")
                known.pop("follower_unavailable_at")
                upsert_sharded(self.frontend / "data", known, "2099-01-01", force=True)
                self.commit(self.frontend, "Publish measured count")
                self.git(self.frontend, "push", "origin", "HEAD:main")
                update = {**source, "tags": ["Adventure"]}
                upsert_sharded(self.frontend / "data", update, "2099-01-01", force=True)
                frozen = freeze_enrichment(self.frontend, appid=123, followers=None,
                                           event_release_date="2099-01-01", force=False,
                                           input_revision=self.git(self.frontend, "rev-parse", "HEAD"))
                self.assertEqual(frozen["records"][0]["followers"], count)
                self.publish(frozen)
                result = self.remote_json("data/games/123.json")
                self.assertEqual(result["followers"], count)
                self.assertEqual(result["tags"], ["Adventure"])
                self.assertNotIn("follower_status", result)
                self.assertNotIn("follower_unavailable_at", result)
                self.assertTrue(is_twitch_qualified(result))

    def test_null_freeze_cannot_claim_an_ordinary_record_without_twitch_proof(self):
        ordinary = record(123)
        upsert_sharded(self.frontend / "data", ordinary, "2099-01-01", force=True)
        with self.assertRaisesRegex(RuntimeError, "does not match its event"):
            freeze_enrichment(self.frontend, appid=123, followers=None,
                              event_release_date="2099-01-01", force=False,
                              input_revision=self.git(self.frontend, "rev-parse", "HEAD"))

    def test_new_official_result_supersedes_old_frozen_unknown_without_overwriting(self):
        frozen = self.freeze_unknown()
        source = frozen["records"][0]
        known = {**source, "followers": 6200, "follower_checked_at": "2026-10-03T00:00:00Z",
                 "follower_source": "Steam Community XML memberCount", "official_ge5000": True}
        known.pop("follower_status")
        known.pop("follower_unavailable_at")
        upsert_sharded(self.other / "data", known, "2099-01-01", force=True)
        self.commit(self.other, "New official measurement")
        self.git(self.other, "push", "origin", "HEAD:main")
        before = self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "main")
        with self.assertRaisesRegex(RuntimeError, "superseded"):
            self.publish(frozen)
        self.assertEqual(self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "main"), before)
        self.assertEqual(self.remote_json("data/games/123.json")["followers"], 6200)

    def test_frozen_reconciliation_counts_and_publishes_unknown_source(self):
        self.freeze_unknown()
        source = json.loads((self.frontend / "data/games/123.json").read_text())
        master = self.root / "master.json"
        write_json(master, {"games": [record(101), source]})
        status = {"generated_at": STAMP, "qualified_master": 2, "complete": True,
                  "pending_count": 0, "pending_enrichment": {}, "failures": {}, "attempted": 1}
        write_json(self.frontend / "data/content_refresh_status.json", status)
        frozen = freeze_reconciliation(self.frontend, master_path=master,
                                       input_revision=self.git(self.frontend, "rev-parse", "HEAD"),
                                       source_revision="a" * 40)
        self.publish(frozen)
        actual = self.remote_json("data/content_refresh_status.json")
        self.assertEqual(actual["qualified_master"], 2)
        self.assertEqual(actual["public_count"], 2)
        self.assertEqual(actual["pending_count"], 0)
        self.assertTrue(actual["complete"])
        self.assertIsNone(self.remote_json("data/games/123.json")["followers"])


class UnknownDispatchTests(unittest.TestCase):
    def test_dispatch_validates_missing_group_and_immutable_twitch_snapshot(self):
        registry, discovery = admission_fixture.DispatchContractTests.snapshots()
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "proof.json"
            payload = {"follower_source": None, "group_id64": None, "official_ge5000": False}
            env = {"APPID": "123", "OFFICIAL_FOLLOWERS": "null", "RELEASE_DATE": "2030-01-01",
                   "OFFICIAL_CHECKED_AT_TAIPEI": "", "TWITCH_ADMISSION_JSON": json.dumps(proof()),
                   "EVENT_ACTION": "steam_game_twitch_discovered", "TWITCH_ADMISSION_PATH": str(destination),
                   "FOLLOWER_STATUS": "unavailable_group_id", "FOLLOWER_UNAVAILABLE_AT": STAMP,
                   "FOLLOWER_STATE_JSON": json.dumps(payload)}
            with patch.dict(os.environ, env), patch("urllib.request.urlopen", side_effect=[io.BytesIO(json.dumps(x).encode()) for x in (registry, discovery)]) as fetch:
                exec(compile(admission_fixture.DispatchContractTests.event_script(), "<unknown event>", "exec"), {})
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(json.loads(destination.read_text()), proof())
            for alterations in ({"FOLLOWER_STATUS": "rate_limited"}, {"FOLLOWER_UNAVAILABLE_AT": "2026-10-02T07:59:59Z"},
                                {"TWITCH_ADMISSION_JSON": "null"}, {"OFFICIAL_CHECKED_AT_TAIPEI": STAMP},
                                {"FOLLOWER_STATE_JSON": json.dumps({**payload, "group_id64": "123"})},
                                {"FOLLOWER_STATE_JSON": json.dumps({**payload, "official_ge5000": True})}):
                with self.subTest(alterations=alterations), patch.dict(os.environ, {**env, **alterations}), patch("urllib.request.urlopen") as fetch:
                    with self.assertRaises(AssertionError):
                        exec(compile(admission_fixture.DispatchContractTests.event_script(), "<bad unknown event>", "exec"), {})
                    fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
