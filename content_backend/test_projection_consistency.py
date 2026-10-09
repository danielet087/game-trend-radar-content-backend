"""Repair real Steam/Content projections whose full-row revision still agrees."""

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import public_catalog as legacy
from radar_core.publication import SubprocessGitRepository, publish_with_retry
from radar_backend.adapters import catalog_projection as canonical
from radar_backend.domain.catalog_projection import catalog_payload, catalog_revision
from radar_backend.publication import catalog
from radar_backend.publication import content_snapshot
from radar_backend.state.json_documents import load_json, write_json

NOW_TEXT = "2026-10-09T16:00:00Z"
NOW = datetime.fromisoformat(NOW_TEXT.replace("Z", "+00:00"))

# Captured from the offline two-producer roundtrip: Content enrichment followed
# by a real Steam public-shards refresh that changes only follower_checked_at.
# Steam preserves this enriched detail, but omits two Content browser fields.
REFRESHED_RECORD = {
    "appid": 9902011, "name": "契約 9902011", "name_en": "Contract 9902011",
    "followers": 6000, "follower_checked_at": "2026-10-09T15:01:00Z",
    "release_start": "2026-11-01", "release_end": "2026-11-01",
    "release_precision": "day", "release_display_precision": "date_full",
    "release_date_timezone": "Asia/Taipei",
    "release_date_verified_at": "2026-10-09T15:00:00Z",
    "sexual_content_screened": True, "steam_type": "game",
    "header_image": "https://example.invalid/image.png",
    "language_support": {"tchinese": True, "english": True},
    "tags": ["Action"], "genres": ["Adventure"], "content_descriptorids": [1],
    "official_ge5000": True,
    "categories": [{"id": 1, "description": "Multi-player"}],
    "categories_source": "Steam Store appdetails cc=TW categories",
    "categories_checked_at": "2026-10-09T15:00:00Z",
    "display_name": "Contract 9902011", "display_name_source": "english",
    "storage_version": 2, "short_description": "這是更新遊戲描述",
    "short_description_language": "zh-TW", "short_description_source": "editorial",
    "description_checked_at": NOW_TEXT, "content_enriched_at": NOW_TEXT,
    "short_description_en": "",
    "content_enrichment_signature": "9902011:6000:2026-11-01",
}
STEAM_PROJECTED_RECORD = {
    "appid": 9902011, "name": "契約 9902011", "name_en": "Contract 9902011",
    "display_name": "Contract 9902011",
    "language_support": {"tchinese": True, "english": True},
    "release_start": "2026-11-01", "release_end": "2026-11-01",
    "release_precision": "day", "release_display_precision": "date_full",
    "release_date_timezone": "Asia/Taipei", "followers": 6000,
    "follower_checked_at": "2026-10-09T15:01:00Z",
    "header_image": "https://example.invalid/image.png", "tags": ["Action"],
    "genres": ["Adventure"], "content_enriched_at": NOW_TEXT,
    "categories": [{"id": 1, "description": "Multi-player"}],
    "categories_source": "Steam Store appdetails cc=TW categories",
    "categories_checked_at": "2026-10-09T15:00:00Z", "steam_type": "game",
    "sexual_content_screened": True,
    "release_date_verified_at": "2026-10-09T15:00:00Z",
}


def steam_projection():
    return {"version": 3, "revision": catalog_revision([REFRESHED_RECORD]),
            "generated_at": NOW_TEXT, "count": 1,
            "games": [copy.deepcopy(STEAM_PROJECTED_RECORD)]}


class ProjectionConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "frontend/data"
        self.no_http = patch("requests.sessions.Session.request",
                             side_effect=AssertionError("Projection repair requested HTTP"))
        self.no_http.start()
        self.addCleanup(self.no_http.stop)

    def seed_steam_refresh(self):
        write_json(self.data / "excluded_appids.json", {"appids": []})
        catalog.upsert_sharded(self.data, copy.deepcopy(REFRESHED_RECORD),
                               REFRESHED_RECORD["release_start"], force=True,
                               generated_at=NOW_TEXT, now=NOW)
        self.assertEqual(load_json(self.data / "games/9902011.json"), REFRESHED_RECORD)
        write_json(self.data / "catalog.json", steam_projection())
        content_snapshot.strict_catalog(self.data, require_index=True)
        content_snapshot.validate_record(REFRESHED_RECORD)

    def test_both_writers_repair_captured_steam_projection_with_identical_revision(self):
        for api in (legacy, canonical):
            with self.subTest(api=api.__name__):
                write_json(self.data / "catalog.json", steam_projection())
                api.write_catalog_projection(self.data, [REFRESHED_RECORD], "repair")
                self.assertEqual(load_json(self.data / "catalog.json"),
                                 catalog_payload([REFRESHED_RECORD], "repair",
                                                 catalog_revision([REFRESHED_RECORD])))

    def test_matching_revision_rejects_wrong_payload_values_and_json_types(self):
        rows = [{"appid": 1, "name": "遊戲", "language_support": {"english": True},
                 "tags": ["Action", "策略"], "content_descriptorids": [1],
                 "official_ge5000": True}]
        expected = catalog_payload(rows, "repair", catalog_revision(rows))
        changes = [
            {"version": 2}, {"version": 3.0}, {"version": True},
            {"count": 0}, {"count": True}, {"count": 1.0},
            {"games": None}, {"games": {}}, {"games": []},
            {"games": [{**expected["games"][0], "appid": True}]},
            {"games": [{**expected["games"][0], "appid": 1.0}]},
            {"games": [{**expected["games"][0], "language_support": {"english": 1}}]},
            {"games": [{**expected["games"][0], "tags": ["策略", "Action"]}]},
            {"games": [{**expected["games"][0], "content_descriptorids": [True]}]},
            {"games": [{**expected["games"][0], "official_ge5000": 1}]},
            {"games": [{**expected["games"][0], "unexpected": "extra"}]},
        ]
        for api in (legacy, canonical):
            for change in changes:
                with self.subTest(api=api.__name__, change=change):
                    write_json(self.data / "catalog.json", {**expected, **change})
                    api.write_catalog_projection(self.data, rows, "repair")
                    self.assertEqual((self.data / "catalog.json").read_bytes(),
                                     (json.dumps(expected, ensure_ascii=False,
                                                 separators=(",", ":")) + "\n").encode())

    def test_valid_payload_ignores_generated_at_and_keeps_exact_bytes_and_mtime(self):
        for api in (legacy, canonical):
            with self.subTest(api=api.__name__):
                payload = catalog_payload([REFRESHED_RECORD], "first",
                                          catalog_revision([REFRESHED_RECORD]))
                payload["unrelated_envelope_note"] = "preserved"
                write_json(self.data / "catalog.json", payload)
                path = self.data / "catalog.json"
                os.utime(path, ns=(1700000000000000000, 1700000000000000000))
                before = path.read_bytes(), path.stat().st_mtime_ns
                api.write_catalog_projection(self.data, [REFRESHED_RECORD], "later")
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_no_force_upsert_repairs_projection_without_touching_enriched_detail(self):
        self.seed_steam_refresh()
        path = self.data / "games/9902011.json"
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertFalse(catalog._shard_in_sync(self.data, REFRESHED_RECORD, now=NOW))
        self.assertTrue(catalog.upsert_sharded(self.data, copy.deepcopy(REFRESHED_RECORD),
                                             REFRESHED_RECORD["release_start"],
                                             generated_at=NOW_TEXT, now=NOW))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        content_snapshot.validate_snapshot(self.data, now=NOW)
        all_json = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.data.rglob("*.json")}
        self.assertFalse(catalog.upsert_sharded(self.data, copy.deepcopy(REFRESHED_RECORD),
                                              REFRESHED_RECORD["release_start"],
                                              generated_at="later", now=NOW))
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in all_json}, all_json)

    def test_shard_consistency_checks_both_index_projection_references(self):
        self.seed_steam_refresh()
        canonical.write_catalog_projection(self.data, [REFRESHED_RECORD], NOW_TEXT)
        index = load_json(self.data / "index.json")
        for change in ({"catalog_path": "old.json"}, {"catalog_revision": "wrong"}):
            with self.subTest(change=change):
                write_json(self.data / "index.json", {**index, **change})
                self.assertFalse(catalog._shard_in_sync(self.data, REFRESHED_RECORD, now=NOW))
                self.assertTrue(catalog.upsert_sharded(self.data, REFRESHED_RECORD,
                                                     REFRESHED_RECORD["release_start"],
                                                     generated_at=NOW_TEXT, now=NOW))
                content_snapshot.validate_snapshot(self.data, now=NOW)

    def test_shard_consistency_checks_all_projected_records(self):
        self.seed_steam_refresh()
        other = {**REFRESHED_RECORD, "appid": 9902012, "name": "Second",
                 "content_enrichment_signature": "9902012:6000:2026-11-01"}
        catalog.upsert_sharded(self.data, other, other["release_start"], force=True,
                               generated_at=NOW_TEXT, now=NOW)
        projection = load_json(self.data / "catalog.json")
        projection["games"][1]["followers"] = 9999
        write_json(self.data / "catalog.json", projection)
        before = {p: p.read_bytes() for p in (self.data / "games").glob("*.json")}
        self.assertFalse(catalog._shard_in_sync(self.data, REFRESHED_RECORD, now=NOW))
        self.assertTrue(catalog.upsert_sharded(self.data, REFRESHED_RECORD,
                                             REFRESHED_RECORD["release_start"],
                                             generated_at=NOW_TEXT, now=NOW))
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        content_snapshot.validate_snapshot(self.data, now=NOW)

    def git(self, cwd, *args):
        return subprocess.check_output(["git", *args], cwd=cwd, text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def test_captured_refresh_repairs_freezes_and_publishes_then_acknowledges_noop(self):
        self.seed_steam_refresh()
        frontend, remote = self.data.parent, self.root / "remote.git"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(remote))
        self.git(frontend, "init", "--initial-branch=main")
        self.git(frontend, "config", "user.name", "Projection repair tests")
        self.git(frontend, "config", "user.email", "tests@example.invalid")
        self.git(frontend, "remote", "add", "origin", str(remote))
        self.git(frontend, "add", ".")
        self.git(frontend, "commit", "-m", "Real Steam projection fixture")
        self.git(frontend, "push", "origin", "HEAD:main")
        original_detail = (self.data / "games/9902011.json").read_bytes()
        self.assertTrue(catalog.upsert_sharded(self.data, REFRESHED_RECORD,
                                             REFRESHED_RECORD["release_start"],
                                             generated_at=NOW_TEXT, now=NOW))

        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz or timezone.utc)

        with patch.object(content_snapshot, "datetime", FixedClock):
            frozen = content_snapshot.freeze_enrichment(
                frontend, appid=9902011, followers=6000,
                event_release_date="2026-11-01", force=False,
                input_revision=self.git(frontend, "rev-parse", "HEAD"))
        unchanged = copy.deepcopy(frozen)
        replay = content_snapshot.FrozenContentPublication(frozen)

        class CountingRepository(SubprocessGitRepository):
            pushes = 0
            def push(self, expected_revision=None):
                self.pushes += 1
                return super().push(expected_revision)

        repo = CountingRepository(frontend, disposable_checkout=True)
        kwargs = dict(paths=content_snapshot.OWNED_PATHS, message="Repair browser projection",
                      input_revision=frozen["input_revision"],
                      payload_revision=replay.payload_revision, max_attempts=2)
        receipt = publish_with_retry(repo, replay, **kwargs)
        self.assertTrue(receipt.changed)
        self.assertEqual(receipt.published_revision,
                         self.git(self.root, "--git-dir", str(remote), "rev-parse", "main"))
        self.assertEqual((self.data / "games/9902011.json").read_bytes(), original_detail)
        content_snapshot.validate_snapshot(self.data, now=NOW)
        self.assertEqual(frozen, unchanged)
        published = {p.relative_to(self.data): p.read_bytes() for p in self.data.rglob("*.json")}
        second = publish_with_retry(repo, replay, **kwargs)
        self.assertFalse(second.changed)
        self.assertEqual(repo.pushes, 2)
        self.assertEqual({p.relative_to(self.data): p.read_bytes() for p in self.data.rglob("*.json")},
                         published)


if __name__ == "__main__":
    unittest.main()
