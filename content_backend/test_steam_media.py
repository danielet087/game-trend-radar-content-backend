"""Steam media files: parsing, weekly refresh and frozen publication."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import refresh_media as cli
from radar_backend.adapters.content_helpers import SteamRateLimit
from radar_backend.domain.content import ASSET_BASE
from radar_backend.domain.steam_media import (MAX_SCREENSHOTS, MAX_TRAILERS, MINIMUM_TIME, VIDEO_BASE, due_at,
                                              media_content, media_document, valid_document)
from radar_backend.publication import catalog
from radar_backend.publication.media_snapshot import FrozenMediaPublication, OWNED_PATHS, freeze
from radar_backend.state.json_documents import load_json, write_json
from radar_core.publication import SubprocessGitRepository, publish_with_retry
from test_content_publication import LocalGitFixture, record

NOW = datetime(2026, 11, 19, 2, 0, tzinfo=timezone.utc)
STAMP = "?t=1794985748"


def trailer(appid, name="PV", key="hash", all_ages=True):
    base = f"{appid}/1239/{key}/1788"
    return {"trailer_name": name, "trailer_url_format": "steam/apps/${FILENAME}?t=1789430727",
            "screenshot_medium": f"257420557/{key}/movie_600x337.jpg",
            "screenshot_full": f"257420557/{key}/movie_full.jpg", "all_ages": all_ages,
            "microtrailer": [{"filename": f"{base}/microtrailer.webm", "type": "video/webm"},
                             {"filename": f"{base}/microtrailer.mp4", "type": "video/mp4"}],
            "adaptive_trailers": [{"cdn_path": f"{base}/dash_av1.mpd", "encoding": "dash_av1"},
                                  {"cdn_path": f"{base}/dash_h264.mpd", "encoding": "dash_h264"},
                                  {"cdn_path": f"{base}/hls_264_master.m3u8", "encoding": "hls_h264"}]}


def item(appid=123, *, trailers=None, screenshots=None, developers=("CAPCOM Co., Ltd.",)):
    return {
        "appid": appid, "success": 1, "visible": True,
        "assets": {"asset_url_format": f"steam/apps/{appid}/${{FILENAME}}{STAMP}",
                   "library_hero": "90ea/library_hero.jpg", "library_hero_2x": "90ea/library_hero_2x.jpg"},
        "basic_info": {"developers": [{"name": name} for name in developers],
                       "publishers": [{"name": "CAPCOM Co., Ltd."}], "franchises": [{"name": "Onimusha"}]},
        "screenshots": {
            "all_ages_screenshots": screenshots if screenshots is not None else [
                {"filename": f"steam/apps/{appid}/aaa/ss_aaa.jpg{STAMP}", "ordinal": 1},
                {"filename": f"steam/apps/{appid}/bbb/ss_bbb.jpg{STAMP}", "ordinal": 0}],
            "mature_content_screenshots": [{"filename": f"steam/apps/{appid}/ccc/ss_ccc.jpg", "ordinal": 2}]},
        "trailers": {"highlights": trailers if trailers is not None else [
            trailer(appid), trailer(appid, "Mature", "adult", all_ages=False)]},
    }


class MediaParsingTests(unittest.TestCase):
    def test_store_item_becomes_hero_screenshots_trailers_and_credits(self):
        content = media_content(123, item())
        self.assertEqual(content["hero_image"], f"{ASSET_BASE}steam/apps/123/90ea/library_hero.jpg{STAMP}")
        self.assertEqual(content["hero_image_2x"], f"{ASSET_BASE}steam/apps/123/90ea/library_hero_2x.jpg{STAMP}")
        self.assertEqual(content["screenshots"], [
            {"thumbnail": f"{ASSET_BASE}steam/apps/123/bbb/ss_bbb.600x338.jpg{STAMP}",
             "full": f"{ASSET_BASE}steam/apps/123/bbb/ss_bbb.jpg{STAMP}"},
            {"thumbnail": f"{ASSET_BASE}steam/apps/123/aaa/ss_aaa.600x338.jpg{STAMP}",
             "full": f"{ASSET_BASE}steam/apps/123/aaa/ss_aaa.jpg{STAMP}"}])
        self.assertEqual(content["trailers"], [{
            "name": "PV",
            "poster": f"{ASSET_BASE}steam/apps/257420557/hash/movie_600x337.jpg?t=1789430727",
            "poster_full": f"{ASSET_BASE}steam/apps/257420557/hash/movie_full.jpg?t=1789430727",
            "dash": f"{VIDEO_BASE}123/1239/hash/1788/dash_h264.mpd",
            "hls": f"{VIDEO_BASE}123/1239/hash/1788/hls_264_master.m3u8",
            "microtrailer_mp4": f"{VIDEO_BASE}123/1239/hash/1788/microtrailer.mp4",
            "microtrailer_webm": f"{VIDEO_BASE}123/1239/hash/1788/microtrailer.webm"}])
        self.assertEqual(content["developers"], ["CAPCOM Co., Ltd."])
        self.assertEqual(content["franchises"], ["Onimusha"])

    def test_foreign_or_traversal_paths_are_dropped(self):
        bad_trailer = trailer(123)
        bad_trailer["microtrailer"] = [{"filename": "../../etc/microtrailer.mp4", "type": "video/mp4"}]
        bad_trailer["adaptive_trailers"] = [{"cdn_path": "https://evil.example/x.m3u8", "encoding": "hls_h264"}]
        row = item(trailers=[bad_trailer], screenshots=[
            {"filename": "https://evil.example/ss.jpg", "ordinal": 0},
            {"filename": "steam/apps/123/../x/ss.jpg", "ordinal": 1},
            {"filename": "steam/apps/123/ok/ss_ok.png", "ordinal": 2}])
        row["assets"]["library_hero"] = "../999/library_hero.jpg"
        content = media_content(123, row)
        self.assertIsNone(content["hero_image"])
        self.assertEqual(content["screenshots"], [])
        self.assertEqual(content["trailers"], [])

    def test_lists_are_capped(self):
        shots = [{"filename": f"steam/apps/123/s{n}/ss_{n}.jpg", "ordinal": n} for n in range(20)]
        clips = [trailer(123, f"T{n}", f"k{n}") for n in range(6)]
        content = media_content(123, item(trailers=clips, screenshots=shots,
                                          developers=[f"Studio {n}" for n in range(9)]))
        self.assertEqual(len(content["screenshots"]), MAX_SCREENSHOTS)
        self.assertEqual([clip["name"] for clip in content["trailers"]], ["T0", "T1", "T2"][:MAX_TRAILERS])
        self.assertEqual(len(content["developers"]), 5)

    def test_item_must_match_appid(self):
        with self.assertRaises(ValueError):
            media_content(456, item(123))

    def test_document_validation_rejects_other_hosts(self):
        document = media_document(123, media_content(123, item()), "2026-11-19T02:00:00Z", "store")
        self.assertTrue(valid_document(document, 123))
        self.assertFalse(valid_document(document, 456))
        document["screenshots"][0]["full"] = "http://example.com/ss.jpg"
        self.assertFalse(valid_document(document, 123))

    def test_due_rules(self):
        self.assertEqual(due_at({}, False), MINIMUM_TIME)
        success = {"outcome": "success", "attempted_at": "2026-11-15T00:00:00Z", "checked_at": "2026-11-15T00:00:00Z"}
        self.assertEqual(due_at(success, True), datetime(2026, 11, 22, tzinfo=timezone.utc))
        self.assertEqual(due_at(success, False), MINIMUM_TIME)
        error = {**success, "outcome": "error", "attempted_at": "2026-11-19T00:00:00Z",
                 "retry_at": "2026-11-19T06:00:00Z"}
        self.assertEqual(due_at(error, True), datetime(2026, 11, 19, 6, tzinfo=timezone.utc))


class MediaRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        write_json(self.data / "excluded_appids.json", {"appids": []})

    def add(self, *appids):
        for appid in appids:
            row = record(appid, release_start="2026-12-01")
            catalog.upsert_sharded(self.data, row, row["release_start"], force=True, now=NOW)

    def fetch(self, **items):
        def lookup(appids, data_request=cli.DATA_REQUEST):
            return {str(appid): items.get(str(appid), item(appid)) for appid in appids}
        return lookup

    def test_first_run_writes_one_media_file_per_game_without_touching_records(self):
        self.add(123, 456)
        before = load_json(self.data / "games/123.json")
        result = cli.refresh(self.data, now=NOW, fetch=self.fetch())
        self.assertEqual(result["updated_appids"], [123, 456])
        self.assertTrue(result["complete"])
        self.assertEqual(result["next_due_at"], "2026-11-26T02:00:00Z")
        document = load_json(self.data / "media/123.json")
        self.assertEqual(document["updated_at"], "2026-11-19T02:00:00Z")
        self.assertEqual(len(document["screenshots"]), 2)
        self.assertEqual(load_json(self.data / "games/123.json"), before)
        self.assertNotIn("screenshots", load_json(self.data / "catalog.json")["games"][0])

    def test_weekly_recheck_keeps_unchanged_files_and_rewrites_changed_ones(self):
        self.add(123, 456)
        cli.refresh(self.data, now=NOW, fetch=self.fetch())
        requests_made = []

        def facts_only(appids, data_request=cli.DATA_REQUEST):
            requests_made.append(data_request)
            return {}

        early = cli.refresh(self.data, now=NOW + timedelta(days=3), fetch=facts_only)
        # Store facts are checked on every run; media only when due.
        self.assertEqual(requests_made, [cli.FACTS_REQUEST])
        self.assertEqual(early["due_count"], 0)
        later = NOW + timedelta(days=8)
        result = cli.refresh(self.data, now=later, fetch=self.fetch(**{"456": item(456, trailers=[trailer(456, "New")])}))
        self.assertEqual(result["updated_appids"], [456])
        self.assertEqual(result["unchanged_count"], 1)
        self.assertEqual(load_json(self.data / "media/123.json")["updated_at"], "2026-11-19T02:00:00Z")
        self.assertEqual(load_json(self.data / "media/456.json")["trailers"][0]["name"], "New")
        self.assertEqual(result["checkpoints"]["123"]["checked_at"], "2026-11-27T02:00:00Z")

    def test_missing_item_keeps_existing_file_and_retries_after_six_hours(self):
        self.add(123)
        cli.refresh(self.data, now=NOW, fetch=self.fetch())
        before = load_json(self.data / "media/123.json")
        later = NOW + timedelta(days=8)
        result = cli.refresh(self.data, now=later, fetch=lambda appids, *_: {})
        self.assertEqual(result["failed_appids"], [123])
        self.assertEqual(load_json(self.data / "media/123.json"), before)
        self.assertEqual(result["checkpoints"]["123"]["retry_at"], "2026-11-27T08:00:00Z")
        self.assertEqual(result["retry_pending_appids"], [123])
        self.assertFalse(result["complete"])

    def test_rate_limit_stops_the_run(self):
        self.add(123)
        def limited(appids, *_):
            raise SteamRateLimit("429")
        result = cli.refresh(self.data, now=NOW, fetch=limited)
        self.assertEqual(result["stop_reason"], "steam_rate_limit")
        self.assertFalse((self.data / "media/123.json").exists())

    def test_excluded_or_removed_games_lose_their_media(self):
        self.add(123, 456)
        cli.refresh(self.data, now=NOW, fetch=self.fetch())
        write_json(self.data / "media/999.json", {"schema_version": 1})
        write_json(self.data / "excluded_appids.json", {"appids": [456]})
        result = cli.refresh(self.data, now=NOW + timedelta(hours=1), fetch=self.fetch())
        self.assertEqual(result["removed_appids"], [456, 999])
        self.assertEqual(sorted(path.name for path in (self.data / "media").glob("*.json")), ["123.json"])

    def test_malformed_file_is_rewritten(self):
        self.add(123)
        write_json(self.data / "media/123.json", {"schema_version": 1, "appid": 123})
        result = cli.refresh(self.data, now=NOW, fetch=self.fetch())
        self.assertEqual(result["updated_appids"], [123])
        self.assertTrue(valid_document(load_json(self.data / "media/123.json"), 123))

    def test_game_budget_leaves_the_rest_pending(self):
        self.add(*range(1, 8))
        batches, fact_batches = [], []
        def lookup(appids, data_request=cli.DATA_REQUEST):
            (batches if data_request is cli.DATA_REQUEST else fact_batches).append(list(appids))
            return {str(appid): item(appid) for appid in appids}
        with patch.object(cli, "BATCH_SIZE", 3):
            result = cli.refresh(self.data, max_games=5, now=NOW, fetch=lookup)
        self.assertEqual(batches, [[1, 2, 3], [4, 5]])
        # The game budget limits media only; store facts cover every game.
        self.assertEqual(fact_batches, [[1, 2, 3], [4, 5, 6], [7]])
        self.assertEqual(result["stop_reason"], "game_budget")
        self.assertEqual(result["pending_count"], 2)

    def test_fetch_requests_assets_screenshots_trailers_and_credits_in_batches(self):
        calls = []
        def request(session, url, *, params, attempts):
            calls.append(json.loads(params["input_json"]))
            return {"response": {"store_items": [item(appid["appid"]) for appid in calls[-1]["ids"]]}}
        with patch.object(cli, "request_json", side_effect=request), patch.object(cli.time, "sleep"):
            rows = cli.fetch_items(list(range(1, 61)))
        self.assertEqual([len(call["ids"]) for call in calls], [50, 10])
        self.assertEqual(calls[0]["data_request"], cli.DATA_REQUEST)
        self.assertEqual(calls[0]["context"]["country_code"], "TW")
        self.assertEqual(len(rows), 60)


class MediaPublicationTests(LocalGitFixture):
    def prepare(self, **items):
        def lookup(appids, *_):
            return {str(appid): items.get(str(appid), item(appid)) for appid in appids}
        cli.refresh(self.frontend / "data", fetch=lookup)
        return freeze(self.frontend, input_revision=self.git(self.frontend, "rev-parse", "HEAD"))

    def publish(self, frozen):
        replay = FrozenMediaPublication(frozen)
        receipt = publish_with_retry(SubprocessGitRepository(self.frontend, disposable_checkout=True), replay,
                                     paths=OWNED_PATHS, message="Media", input_revision=frozen["input_revision"],
                                     payload_revision=replay.payload_revision, max_attempts=3)
        return receipt, replay

    def test_media_is_published_beside_concurrent_record_changes(self):
        frozen = self.prepare()
        newer = record(101, followers=9000, follower_checked_at="2026-10-15T01:00:00Z",
                       content_enriched_at="2026-10-15T01:00:00Z")
        catalog.upsert_sharded(self.other / "data", newer, newer["release_start"], force=True)
        self.commit(self.other, "Concurrent official evidence")
        self.git(self.other, "push", "origin", "HEAD:main")
        _, replay = self.publish(frozen)
        self.assertEqual(self.remote_json("data/games/101.json")["followers"], 9000)
        self.assertEqual(self.remote_json("data/media/101.json")["developers"], ["CAPCOM Co., Ltd."])
        self.assertEqual(self.remote_json("data/publication/steam_media.json")["operation"], "steam_media")
        self.assertEqual(replay.result["updated_appids"], [101])
        self.assertTrue(replay.result["complete"])

    def test_newer_media_on_main_supersedes_the_frozen_file(self):
        frozen = self.prepare()
        newer = media_document(101, media_content(101, item(101, trailers=[trailer(101, "Newest")])),
                               "2099-01-01T00:00:00Z", "store")
        write_json(self.other / "data/media/101.json", newer)
        self.commit(self.other, "Newer media")
        self.git(self.other, "push", "origin", "HEAD:main")
        _, replay = self.publish(frozen)
        self.assertEqual(self.remote_json("data/media/101.json")["trailers"][0]["name"], "Newest")
        self.assertEqual(replay.result["superseded_appids"], [101])

    def test_a_game_excluded_meanwhile_gets_no_media(self):
        frozen = self.prepare()
        write_json(self.other / "data/excluded_appids.json", {"appids": [101]})
        self.commit(self.other, "Adult audit exclusion")
        self.git(self.other, "push", "origin", "HEAD:main")
        _, replay = self.publish(frozen)
        self.assertEqual(replay.result["superseded_appids"], [101])
        listing = self.git(self.root, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "main", "data/media")
        self.assertEqual(listing, "")

    def test_nothing_due_skips_publication(self):
        from radar_backend.jobs import publish_media
        self.prepare()
        frozen = freeze(self.frontend, input_revision=self.git(self.frontend, "rev-parse", "HEAD"))
        frozen["documents"], frozen["store_facts"] = [], None
        frozen["status"].update(due_count=0, removed_appids=[], updated_appids=[])
        batch, receipt = self.root / "batch.json", self.root / "receipt.json"
        write_json(batch, frozen)
        head = self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "main")
        argv = ["publish_media", "publish", "--frontend", str(self.frontend), "--frozen", str(batch),
                "--receipt", str(receipt)]
        with patch("sys.argv", argv):
            publish_media.main()
        self.assertEqual(load_json(receipt)["job_result"]["status"], "skipped")
        self.assertEqual(self.git(self.root, "--git-dir", str(self.remote), "rev-parse", "main"), head)

    def test_malformed_frozen_batches_are_rejected(self):
        frozen = self.prepare()
        frozen["documents"][0]["hero_image"] = "https://evil.example/hero.jpg"
        with self.assertRaises(RuntimeError):
            FrozenMediaPublication(frozen)


if __name__ == "__main__":
    unittest.main()
