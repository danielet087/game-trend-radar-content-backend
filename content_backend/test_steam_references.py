"""Steam versions of console games: links, titles, refresh and publication."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

import refresh_media
import refresh_references as cli
from radar_backend.adapters.steam_store import SteamRateLimit
from radar_backend.domain.steam_references import (MINIMUM_TIME, console_links, current_records, due_at,
                                                   link_signature, reference_release, steam_link, title_match,
                                                   valid_document)
from radar_backend.publication import catalog
from radar_backend.publication.media_snapshot import FrozenMediaPublication, OWNED_PATHS, freeze
from radar_backend.state.json_documents import load_json, write_json
from radar_core.publication import SubprocessGitRepository, publish_with_retry
from test_content_publication import LocalGitFixture, record
from test_steam_media import item as media_item

NOW = datetime(2026, 11, 19, 2, 0, tzinfo=timezone.utc)
RELEASED = 1718236800  # 2024-06-13 00:00 UTC


def console(igdb_id, name, *urls, steam_appid=None):
    row = {"igdb_id": igdb_id, "id": f"igdb:{igdb_id}", "name_en": name,
           "websites": [{"url": url} for url in urls]}
    if steam_appid is not None:
        row["steam_appid"] = steam_appid
    return row


def store(appid):
    return f"https://store.steampowered.com/app/{appid}/slug"


def browse(appid, name, *, tagids=(19, 21), descriptors=(), coming_soon=False, display=None, stamp=RELEASED,
           short="An English description of the game.", success=1):
    return {"appid": appid, "success": success, "visible": True, "name": name,
            "assets": {"asset_url_format": f"steam/apps/{appid}/${{FILENAME}}?t=1",
                       "header": "header.jpg", "main_capsule": "capsule_616x353.jpg"},
            "release": {"steam_release_date": stamp, "is_coming_soon": coming_soon,
                        **({"coming_soon_display": display} if display else {})},
            "supported_languages": [{"elanguage": 0, "supported": True}, {"elanguage": 7, "supported": True}],
            "categories": {"supported_player_categoryids": [2, 9]},
            "tagids": list(tagids), "content_descriptorids": list(descriptors),
            "basic_info": {"short_description": short}}


def details(appid, *, kind="game", date="2024 年 6 月 13 日", coming_soon=False, descriptors=()):
    return {"type": kind, "steam_appid": appid, "name": f"App {appid}",
            "release_date": {"coming_soon": coming_soon, "date": date},
            "genres": [{"id": "1", "description": "動作"}, {"id": "25", "description": "冒險"}],
            "short_description": "一款中文介紹的遊戲，玩家將在世界中冒險。",
            "content_descriptors": {"ids": list(descriptors)}}


class FakeClient:
    def __init__(self, items=None, details=None, *, limited_after=None):
        self.items, self.detail_rows, self.calls, self.requests = items or {}, details or {}, [], 0
        self.limited_after = limited_after

    def browse(self, appids, language):
        self.calls.append(("browse", language, list(appids)))
        self.requests += 1
        rows = {}
        for appid in appids:
            row = self.items.get(appid)
            if row is not None:
                row = dict(row)
                if language == "tchinese":
                    row["name"] = row.get("name_tw", row["name"])
                    row["basic_info"] = {"short_description": row.get("short_tw", "這是一款繁體中文介紹的遊戲，介紹它的世界。")}
                if language == "schinese":
                    row["name"] = row.get("name_cn", row["name"])
                rows[appid] = row
        return rows

    def details(self, appid, language="tchinese"):
        self.calls.append(("details", appid))
        self.requests += 1
        if self.limited_after is not None and len([call for call in self.calls if call[0] == "details"]) > self.limited_after:
            raise SteamRateLimit("429")
        return self.detail_rows.get(appid, details(appid))

    def tag_names(self, language):
        return {19: "Action", 21: "Adventure"} if language == "english" else {19: "動作", 21: "冒險"}


class LinkAndTitleTests(unittest.TestCase):
    def test_one_store_product_is_a_link(self):
        self.assertEqual(steam_link(console(1, "A", store(10), "https://example.com")), 10)
        self.assertEqual(steam_link(console(1, "A", "https://store.steampowered.com/app/10?l=tchinese")), 10)
        self.assertIsNone(steam_link(console(1, "A", store(10), store(11))))
        self.assertIsNone(steam_link(console(1, "A", "http://store.steampowered.com/app/10")))
        self.assertIsNone(steam_link(console(1, "A", store(10), steam_appid=11)))
        self.assertEqual(steam_link(console(1, "A", store(10), steam_appid=10)), 10)

    def test_console_links_group_games_by_product(self):
        native = {"schema_version": 1, "games": [console(2, "B", store(10)), console(1, "A", store(10)),
                                                 console(3, "C"), {"igdb_id": "x"}]}
        self.assertEqual(console_links(native), {10: [{"igdb_id": 1, "name_en": "A"}, {"igdb_id": 2, "name_en": "B"}]})
        with self.assertRaises(ValueError):
            console_links({"schema_version": 2, "games": []})

    def test_titles(self):
        cases = [
            ("Kingdom Hearts III", "KINGDOM HEARTS III + Re Mind (DLC)", "subtitle"),
            ("Mafia III", "Mafia III: Definitive Edition", "subtitle"),
            ("Dragon's Dogma II", "Dragon's Dogma 2", "exact"),
            ("Stellar Blade", "Stellar Blade™", "exact"),
            ("Epic Mickey: Rebrushed", "Disney Epic Mickey: Rebrushed", "contained"),
            ("Toy Story 3: Complete Edition", "Disney•Pixar Toy Story 3 Complete Edition", "contained"),
            ("Everything Is Crab", "Everything is Crab: The Animal Evolution Roguelite", "subtitle"),
            ("Attack on Titan 3", "Attack on Titan 3 / A.O.T. 3", "subtitle"),
            ("Atelier Karia: The Night Kingdom & the Guide of Memories",
             "Atelier Karia: The Night Kingdom and the Guide of Memories", "exact"),
            ("Kingdom Hearts Birth by Sleep Final Mix", "KINGDOM HEARTS -HD 1.5+2.5 ReMIX-", None),
            ("Kingdom Hearts Dream Drop Distance HD", "KINGDOM HEARTS HD 2.8 Final Chapter Prologue", None),
            ("Hela", "Hela 2", None),
            ("Doom", "DOOM Eternal", None),
            ("Resident Evil", "Resident Evil 4", None),
            ("", "Anything", None),
        ]
        for console_name, steam_name, expected in cases:
            with self.subTest(console_name=console_name, steam_name=steam_name):
                self.assertEqual(title_match(console_name, steam_name), expected)


class ReleaseAndDueTests(unittest.TestCase):
    def test_released_game_uses_the_taiwan_store_day(self):
        release = reference_release(browse(1, "A"), details(1), NOW)
        self.assertEqual(release["release_start"], "2024-06-13")
        self.assertFalse(release["release_coming_soon"])
        self.assertEqual(release["release_date_basis"], "steam_taiwan_store_date")

    def test_upcoming_game_needs_an_exact_announced_day(self):
        future = int(datetime(2027, 3, 3, 16, tzinfo=timezone.utc).timestamp())
        exact = reference_release(browse(1, "A", coming_soon=True, display="date_full", stamp=future),
                                  details(1, coming_soon=True, date="2027 年 3 月 4 日"), NOW)
        self.assertEqual((exact["release_start"], exact["release_coming_soon"]), ("2027-03-04", True))
        stamp_only = reference_release(browse(1, "A", coming_soon=True, display="date_full", stamp=future),
                                       details(1, coming_soon=True, date="即將推出"), NOW)
        self.assertEqual((stamp_only["release_start"], stamp_only["release_date_basis"]),
                         ("2027-03-04", "steam_release_timestamp"))
        vague = reference_release(browse(1, "A", coming_soon=True, display="date_month", stamp=future),
                                  details(1, coming_soon=True, date="2027 年 3 月"), NOW)
        self.assertIsNone(vague["release_start"])
        self.assertIsNone(vague["release_precision"])

    def test_due_rules(self):
        links = [{"igdb_id": 1, "name_en": "A"}]
        self.assertEqual(due_at({}, links, False), MINIMUM_TIME)
        accepted = {"links": link_signature(links), "outcome": "accepted",
                    "attempted_at": "2026-11-19T02:00:00Z", "checked_at": "2026-11-19T02:00:00Z"}
        self.assertEqual(due_at(accepted, links, True), NOW + timedelta(days=3))
        self.assertEqual(due_at(accepted, links, False), MINIMUM_TIME)
        self.assertEqual(due_at(accepted, [{"igdb_id": 1, "name_en": "Renamed"}], True), MINIMUM_TIME)
        mismatch = {**accepted, "outcome": "title_mismatch"}
        self.assertEqual(due_at(mismatch, links, False), NOW + timedelta(days=3))
        error = {**accepted, "outcome": "error", "attempted_at": "2026-11-20T00:00:00Z",
                 "retry_at": "2026-11-20T06:00:00Z"}
        self.assertEqual(due_at(error, links, True), datetime(2026, 11, 20, 6, tzinfo=timezone.utc))


class ReferenceData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        write_json(self.data / "excluded_appids.json", {"appids": []})
        row = record(101, release_start="2026-12-01")
        catalog.upsert_sharded(self.data, row, row["release_start"], force=True, now=NOW)
        self.games = [console(1, "Kingdom Hearts III", store(500)),
                      console(2, "Kingdom Hearts Dream Drop Distance HD", store(600)),
                      console(3, "Kingdom Hearts 0.2", store(600)),
                      console(4, "Game 101", store(101)),
                      console(5, "Adult Game", store(700)),
                      console(6, "No Steam")]
        self.write_console()
        self.items = {500: {**browse(500, "KINGDOM HEARTS III + Re Mind (DLC)"), "name_tw": "王國之心 III"},
                      600: browse(600, "KINGDOM HEARTS HD 2.8 Final Chapter Prologue"),
                      700: browse(700, "Adult Game", descriptors=(3,))}

    def write_console(self):
        write_json(self.data / "nintendo_upcoming.json", {"schema_version": 1, "games": self.games})

    def run_refresh(self, client=None, now=NOW, **budget):
        return cli.refresh(self.data, now=now, client=client or FakeClient(self.items), **budget)


class RefreshTests(ReferenceData):
    def test_only_same_titled_unlisted_screened_products_get_reference_rows(self):
        client = FakeClient(self.items)
        report = self.run_refresh(client)
        document = load_json(self.data / "steam_references.json")
        self.assertTrue(valid_document(document))
        self.assertEqual([row["appid"] for row in document["games"]], [500])
        row = document["games"][0]
        self.assertEqual(row["display_name"], "王國之心 III")
        self.assertEqual(row["name_en"], "KINGDOM HEARTS III + Re Mind (DLC)")
        self.assertEqual(row["reference"], {"method": "igdb_console_steam_link_v1", "igdb_ids": [1],
                                            "console_titles": ["Kingdom Hearts III"], "title_match": "subtitle"})
        self.assertEqual((row["release_start"], row["release_coming_soon"]), ("2024-06-13", False))
        self.assertIsNone(row["followers"])
        self.assertEqual(row["short_description_source"], "steam_tchinese")
        self.assertEqual(row["tags"], ["Action", "Adventure"])
        self.assertEqual(row["tag_labels_zh_tw"], {"Action": "動作", "Adventure": "冒險"})
        self.assertEqual(row["genres"], ["Action", "Adventure"])
        self.assertEqual(row["language_support"], {"tchinese": True, "schinese": False, "english": True})
        self.assertEqual([category["id"] for category in row["categories"]], [2, 9])
        self.assertTrue(row["header_image"].endswith("/steam/apps/500/header.jpg?t=1"))
        outcomes = {key: value["outcome"] for key, value in report["checkpoints"].items()}
        # 101 is already in the Steam catalog; "No Steam" names no product.
        self.assertEqual(outcomes, {"500": "accepted", "600": "title_mismatch", "700": "excluded"})
        # The mismatched collection costs no appdetails request.
        self.assertEqual([call for call in client.calls if call[0] == "details"], [("details", 500), ("details", 700)])
        self.assertTrue(report["complete"])
        self.assertEqual(report["next_due_at"], "2026-11-22T02:00:00Z")

    def test_file_is_compact_and_unchanged_runs_do_not_rewrite_it(self):
        self.run_refresh()
        text = (self.data / "steam_references.json").read_text()
        self.assertNotIn("\n  ", text)
        before = (self.data / "steam_references.json").stat().st_mtime_ns
        report = self.run_refresh(FakeClient(self.items), now=NOW + timedelta(hours=6))
        self.assertEqual(report["due_count"], 0)
        self.assertFalse(report["changed"])
        self.assertEqual((self.data / "steam_references.json").stat().st_mtime_ns, before)

    def test_console_rename_rechecks_at_once_and_drops_a_mismatch(self):
        self.run_refresh()
        self.games[0] = console(1, "Kingdom Hearts IV", store(500))
        self.write_console()
        report = self.run_refresh(FakeClient(self.items), now=NOW + timedelta(hours=1))
        self.assertEqual(report["checkpoints"]["500"]["outcome"], "title_mismatch")
        self.assertEqual(load_json(self.data / "steam_references.json")["games"], [])

    def test_a_product_entering_the_steam_catalog_loses_its_reference(self):
        self.run_refresh()
        row = record(500, release_start="2026-12-01")
        catalog.upsert_sharded(self.data, row, row["release_start"], force=True, now=NOW)
        report = self.run_refresh(FakeClient(self.items), now=NOW + timedelta(hours=1))
        self.assertEqual(report["candidate_count"], 2)
        self.assertEqual(load_json(self.data / "steam_references.json")["games"], [])
        self.assertNotIn("500", report["checkpoints"])

    def test_transient_failure_keeps_the_published_row_and_retries(self):
        self.run_refresh()
        later = NOW + timedelta(days=4)
        report = self.run_refresh(FakeClient(self.items, {500: {}, 700: {}}), now=later)
        self.assertEqual(report["checkpoints"]["500"]["outcome"], "error")
        self.assertEqual(report["checkpoints"]["500"]["retry_at"], "2026-11-23T08:00:00Z")
        self.assertEqual([row["appid"] for row in load_json(self.data / "steam_references.json")["games"]], [500])

    def test_rate_limit_stops_without_losing_rows(self):
        self.run_refresh()
        report = self.run_refresh(FakeClient(self.items, limited_after=0), now=NOW + timedelta(days=4))
        self.assertEqual(report["stop_reason"], "steam_rate_limit")
        self.assertEqual([row["appid"] for row in load_json(self.data / "steam_references.json")["games"]], [500])

    def test_budget_leaves_the_rest_due(self):
        report = self.run_refresh(max_games=1)
        self.assertEqual(report["stop_reason"], "game_budget")
        self.assertEqual(report["pending_count"], 2)

    def test_missing_console_catalog_changes_nothing(self):
        self.run_refresh()
        before = load_json(self.data / "steam_references.json")
        (self.data / "nintendo_upcoming.json").unlink()
        report = self.run_refresh(FakeClient(self.items), now=NOW + timedelta(days=4))
        self.assertEqual(report["stop_reason"], "console_catalog_unavailable")
        self.assertEqual(load_json(self.data / "steam_references.json"), before)

    def test_records_follow_the_latest_console_and_steam_catalogs(self):
        self.run_refresh()
        rows = load_json(self.data / "steam_references.json")["games"]
        links = console_links(load_json(self.data / "nintendo_upcoming.json"))
        self.assertEqual(current_records(rows, links, {101}, set()), rows)
        self.assertEqual(current_records(rows, links, {101, 500}, set()), [])
        self.assertEqual(current_records(rows, links, {101}, {500}), [])
        self.assertEqual(current_records(rows, {}, {101}, set()), [])

    def test_media_and_store_facts_cover_reference_products(self):
        self.run_refresh()

        def lookup(appids, data_request=refresh_media.DATA_REQUEST):
            return {str(appid): media_item(appid) for appid in appids}
        result = refresh_media.refresh(self.data, now=NOW, fetch=lookup)
        self.assertEqual(result["updated_appids"], [101, 500])
        self.assertTrue((self.data / "media/500.json").is_file())
        self.games[0] = console(1, "Kingdom Hearts III")
        self.write_console()
        self.run_refresh(FakeClient(self.items), now=NOW + timedelta(hours=1))
        result = refresh_media.refresh(self.data, now=NOW + timedelta(hours=1), fetch=lookup)
        self.assertEqual(result["removed_appids"], [500])


class ReferencePublicationTests(LocalGitFixture):
    def setUp(self):
        super().setUp()
        self.games = [console(1, "Kingdom Hearts III", store(500))]
        write_json(self.frontend / "data/nintendo_upcoming.json", {"schema_version": 1, "games": self.games})
        self.commit(self.frontend, "Console catalog")
        self.git(self.frontend, "push", "origin", "HEAD:main")
        self.git(self.other, "pull", "--ff-only", "origin", "main")

    def prepare(self):
        data = self.frontend / "data"
        cli.refresh(data, client=FakeClient({500: browse(500, "KINGDOM HEARTS III + Re Mind (DLC)")}))
        refresh_media.refresh(data, fetch=lambda appids, *_: {str(appid): media_item(appid) for appid in appids})
        return freeze(self.frontend, input_revision=self.git(self.frontend, "rev-parse", "HEAD"))

    def publish(self, frozen):
        replay = FrozenMediaPublication(frozen)
        publish_with_retry(SubprocessGitRepository(self.frontend, disposable_checkout=True), replay,
                           paths=OWNED_PATHS, message="Media", input_revision=frozen["input_revision"],
                           payload_revision=replay.payload_revision, max_attempts=3)
        return replay

    def test_references_media_and_status_are_published_together(self):
        replay = self.publish(self.prepare())
        self.assertEqual([row["appid"] for row in self.remote_json("data/steam_references.json")["games"]], [500])
        self.assertEqual(self.remote_json("data/steam_reference_status.json")["checkpoints"]["500"]["outcome"], "accepted")
        self.assertEqual(self.remote_json("data/media/500.json")["appid"], 500)
        self.assertEqual(replay.result["references"], "written")
        self.assertEqual(replay.result["reference_count"], 1)

    def test_a_console_game_removed_meanwhile_takes_its_reference_and_media_along(self):
        frozen = self.prepare()
        write_json(self.other / "data/nintendo_upcoming.json", {"schema_version": 1, "games": []})
        self.commit(self.other, "Console game left the public catalog")
        self.git(self.other, "push", "origin", "HEAD:main")
        replay = self.publish(frozen)
        self.assertEqual(self.remote_json("data/steam_references.json")["games"], [])
        self.assertEqual(self.remote_json("data/steam_reference_status.json")["checkpoints"], {})
        self.assertIn(500, replay.result["superseded_appids"])
        listing = self.git(self.root, "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "main", "data/media")
        self.assertEqual(listing, "data/media/101.json")

    def test_malformed_frozen_references_are_rejected(self):
        frozen = self.prepare()
        frozen["references"]["games"][0]["followers"] = 9000
        with self.assertRaises(RuntimeError):
            FrozenMediaPublication(frozen)


if __name__ == "__main__":
    unittest.main()
