"""Store facts: Taiwan price, CSRR rating and Steam Deck status for every game."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

import refresh_media as cli
from radar_backend.domain.steam_store_facts import (advance_access, descriptors, early_access, facts_document,
                                                    needs_publication, price, rating, steam_deck, store_facts,
                                                    valid_document)
from radar_backend.publication import catalog
from radar_backend.publication.media_snapshot import freeze
from radar_backend.state.json_documents import load_json, write_json
from test_steam_media import MediaPublicationTests, NOW, item

STORE = {"is_free": False,
         "best_purchase_option": {"final_price_in_cents": "44800", "original_price_in_cents": "56100",
                                  "formatted_final_price": "NT$ 448.00", "discount_pct": 20,
                                  "hide_discount_pct_for_compliance": False,
                                  "active_discounts": [{"discount_end_date": 1796061600}]},
         "game_rating": {"agency": 13, "rating": "C15", "descriptors": ["Violence, Inappropriate", "language"]},
         "platforms": {"steam_deck_compat_category": 3}}


def store_item(appid=123, **changes):
    return {**item(appid), **STORE, **changes}


class StoreFactParsingTests(unittest.TestCase):
    def test_price_rating_and_deck_from_one_store_item(self):
        facts = store_facts(123, store_item())
        self.assertEqual(facts, {
            "price": {"free": False, "final_cents": 44800, "original_cents": 56100, "discount_pct": 20,
                      "discount_ends_at": "2026-11-30T18:00:00Z"},
            "rating": {"code": "C15", "label": "輔15級", "descriptors": ["暴力", "不當言語"]},
            "steam_deck": "verified", "advance_access": None, "early_access": None})

    def test_descriptors_normalize_chinese_english_split_and_joined_names(self):
        self.assertEqual(descriptors(["性,", "暴力,", "不當言語"]), ["性", "暴力", "不當言語"])
        self.assertEqual(descriptors(["Tobacco and Alcohol", "Sex, Violence, Fear"]), ["性", "暴力", "恐怖", "菸酒"])
        self.assertEqual(descriptors(["性,暴力,反社會性,戀愛交友", "drugs"]), ["性", "暴力", "毒品", "反社會性", "戀愛交友"])
        self.assertEqual(descriptors(["牌類及益智娛樂"]), ["牌類及益智娛樂"])
        self.assertEqual(descriptors("Violence"), [])

    def test_only_known_csrr_codes_become_ratings(self):
        self.assertEqual(rating({"game_rating": {"agency": 13, "rating": "G", "descriptors": []}})["label"], "普遍級")
        self.assertEqual(rating({"game_rating": {"agency": 13, "rating": "P6"}})["label"], "保護級")
        self.assertIsNone(rating({"game_rating": {"agency": 1, "rating": "M"}}))
        self.assertIsNone(rating({"game_rating": {"agency": 13, "rating": "X"}}))
        self.assertIsNone(rating({"game_rating": {"content_survey_notes": "Mature"}}))

    def test_price_free_none_hidden_discount_and_malformed(self):
        self.assertEqual(price({"is_free": True}), {"free": True})
        self.assertIsNone(price({}))
        self.assertIsNone(price({"best_purchase_option": {"final_price_in_cents": "abc"}}))
        hidden = {"best_purchase_option": {**STORE["best_purchase_option"], "hide_discount_pct_for_compliance": True}}
        self.assertEqual(price(hidden), {"free": False, "final_cents": 44800})
        plain = {"best_purchase_option": {"final_price_in_cents": "199000"}}
        self.assertEqual(price(plain), {"free": False, "final_cents": 199000})

    def test_steam_deck_categories(self):
        self.assertEqual([steam_deck({"platforms": {"steam_deck_compat_category": value}}) for value in (0, 1, 2, 3, None)],
                         [None, "unsupported", "playable", "verified", None])

    def test_wrong_appid_is_rejected(self):
        with self.assertRaises(ValueError):
            store_facts(456, store_item(123))

    def test_unchanged_facts_republish_only_after_twenty_hours(self):
        entry = {**store_facts(123, store_item()), "checked_at": "2026-11-19T02:00:00Z"}
        previous = facts_document({"123": entry}, "2026-11-19T02:00:00Z", "store")
        later = {**entry, "checked_at": "2026-11-19T08:00:00Z"}
        current = facts_document({"123": later}, "2026-11-19T08:00:00Z", "store")
        self.assertFalse(needs_publication(previous, current, NOW + timedelta(hours=6)))
        self.assertTrue(needs_publication(previous, current, NOW + timedelta(hours=20)))
        cheaper = facts_document({"123": {**later, "price": {"free": False, "final_cents": 1}}}, later["checked_at"], "store")
        self.assertTrue(needs_publication(previous, cheaper, NOW + timedelta(hours=6)))
        self.assertTrue(needs_publication(None, current, NOW))

    def test_document_validation(self):
        entry = {**store_facts(123, store_item()), "checked_at": "2026-11-19T02:00:00Z"}
        self.assertTrue(valid_document(facts_document({"123": entry}, "2026-11-19T02:00:00Z", "store")))
        early = {**entry, "advance_access": {"starts_at": "2026-12-01T17:00:00Z", "editions": ["Deluxe"]}}
        self.assertTrue(valid_document(facts_document({"123": early}, "2026-11-19T02:00:00Z", "store")))
        for broken in ({**entry, "steam_deck": "great"}, {**entry, "checked_at": "soon"},
                       {**entry, "rating": {"code": "R", "label": "普遍級", "descriptors": []}},
                       {**entry, "price": {"free": False, "final_cents": 10, "original_cents": 5, "discount_pct": 50}},
                       {**entry, "advance_access": {"starts_at": "soon", "editions": []}},
                       {**entry, "advance_access": {"starts_at": "2026-12-01T17:00:00Z", "editions": [""]}},
                       {**entry, "advance_access": {"starts_at": "2026-12-01T17:00:00Z"}},
                       {**entry, "advance_access": {"starts_at": "2026-12-01T17:00:00Z", "editions": [],
                                                    "days": 3}},
                       {**entry, "early_access": {"active": "yes", "started_at": None, "full_release_at": None}},
                       {**entry, "early_access": {"active": True, "started_at": "soon", "full_release_at": None}},
                       {**entry, "early_access": {"active": True, "started_at": None}},
                       {**entry, "early_access": {"active": False, "started_at": "2026-12-01T17:00:00Z",
                                                  "full_release_at": "2026-11-01T17:00:00Z"}}):
            self.assertFalse(valid_document(facts_document({"123": broken}, "2026-11-19T02:00:00Z", "store")))
        older = {key: value for key, value in entry.items() if key != "early_access"}
        self.assertTrue(valid_document(facts_document({"123": older}, "2026-11-19T02:00:00Z", "store")))


RELEASE = 1796403600  # 2026-12-04T17:00:00Z
EDITIONS = [{"packageid": 11, "purchase_option_name": "Game"},
            {"packageid": 12, "purchase_option_name": "Game Deluxe Edition", "is_edition": True},
            {"bundleid": 13, "purchase_option_name": "Game + Soundtrack"}]


def upcoming(appid=123, **release):
    return store_item(appid, purchase_options=EDITIONS, best_purchase_option=EDITIONS[0],
                      release={"is_coming_soon": True, "steam_release_date": RELEASE, **release})


def package(packageid, advance=None):
    return {"item_type": 1, "id": packageid, "success": 1, "name": f"Package {packageid}",
            "release": {} if advance is None else {"advance_access_date": advance}}


class AdvanceAccessTests(unittest.TestCase):
    def test_edition_packages_give_the_start_and_the_editions(self):
        packages = {"11": package(11), "12": package(12, RELEASE - 3 * 86400)}
        self.assertEqual(advance_access(upcoming(), packages),
                         {"starts_at": "2026-12-01T17:00:00Z", "editions": ["Game Deluxe Edition"]})
        self.assertEqual(store_facts(123, upcoming(), packages)["advance_access"]["starts_at"],
                         "2026-12-01T17:00:00Z")

    def test_app_release_alone_or_unnamed_package(self):
        self.assertEqual(advance_access(upcoming(advance_access_date=str(RELEASE - 86400)), {}),
                         {"starts_at": "2026-12-03T17:00:00Z", "editions": []})
        item = upcoming()
        item["purchase_options"] = [{"packageid": 12}]
        item["best_purchase_option"] = None
        self.assertEqual(advance_access(item, {"12": package(12, RELEASE - 86400)})["editions"], ["Package 12"])

    def test_no_or_invalid_start_is_none(self):
        self.assertIsNone(advance_access(upcoming(), {"11": package(11), "12": package(12)}))
        self.assertIsNone(advance_access(upcoming(), None))
        for value in (True, "soon", -5, 10 ** 12, RELEASE, RELEASE + 60):
            with self.subTest(value=value):
                self.assertIsNone(advance_access(upcoming(), {"12": package(12, value)}))


EA_START = 1789905600  # 2026-09-20T12:00:00Z
FULL = 1820000000  # 2027-09-03T19:33:20Z


class EarlyAccessTests(unittest.TestCase):
    def test_a_game_in_early_access_and_its_planned_full_release(self):
        self.assertEqual(early_access(store_item(release={"is_early_access": True, "steam_release_date": EA_START})),
                         {"active": True, "started_at": "2026-09-20T12:00:00Z", "full_release_at": None})
        planned = store_item(is_early_access=True, release={"steam_release_date": EA_START,
                                                            "release_from_early_access_date": FULL})
        self.assertEqual(early_access(planned),
                         {"active": True, "started_at": "2026-09-20T12:00:00Z",
                          "full_release_at": "2027-09-03T19:33:20Z"})
        self.assertEqual(store_facts(123, planned)["early_access"]["full_release_at"], "2027-09-03T19:33:20Z")

    def test_a_game_that_left_early_access(self):
        left = store_item(release={"is_early_access": False, "steam_release_date": FULL,
                                   "original_steam_release_date": EA_START,
                                   "release_from_early_access_date": FULL})
        self.assertEqual(early_access(left), {"active": False, "started_at": "2026-09-20T12:00:00Z",
                                              "full_release_at": "2027-09-03T19:33:20Z"})
        # Without the Early Access fields, the last record gives the start and
        # the Steam date is the full release.
        earlier = {"active": True, "started_at": "2026-09-20T12:00:00Z", "full_release_at": None}
        self.assertEqual(early_access(store_item(release={"steam_release_date": FULL}), earlier),
                         {"active": False, "started_at": "2026-09-20T12:00:00Z",
                          "full_release_at": "2027-09-03T19:33:20Z"})

    def test_games_never_in_early_access_and_bad_values(self):
        self.assertIsNone(early_access(store_item()))
        self.assertIsNone(store_facts(123, store_item())["early_access"])
        # An earlier Steam date alone does not make a game Early Access.
        self.assertIsNone(early_access(store_item(release={"steam_release_date": FULL,
                                                           "original_steam_release_date": EA_START})))
        for value in ("soon", -5, 10 ** 12, True, EA_START, EA_START - 60):
            with self.subTest(value=value):
                record = early_access(store_item(release={"is_early_access": True, "steam_release_date": EA_START,
                                                          "release_from_early_access_date": value}))
                self.assertIsNone(record["full_release_at"])
        self.assertEqual(early_access(store_item(release={"is_early_access": True}),
                                      {"active": True, "started_at": None, "full_release_at": None}),
                         {"active": True, "started_at": None, "full_release_at": None})


class StoreFactRefreshTests(MediaPublicationTests):
    def lookup(self, **items):
        def fetch(appids, data_request=cli.DATA_REQUEST):
            return {str(appid): items.get(str(appid), store_item(appid)) for appid in appids}
        return fetch

    def test_every_run_checks_all_games_and_failures_keep_last_values(self):
        data = self.frontend / "data"
        first = cli.refresh(data, now=NOW, fetch=self.lookup())
        self.assertEqual(first["facts_failed_appids"], [])
        self.assertTrue(first["facts_publish"])
        facts = load_json(data / "store_facts.json")
        self.assertEqual(facts["games"]["101"]["checked_at"], "2026-11-19T02:00:00Z")

        def broken(appids, data_request=cli.DATA_REQUEST):
            raise RuntimeError("Steam is down")
        later = cli.refresh(data, now=NOW + timedelta(hours=6), fetch=broken)
        self.assertEqual(later["facts_failed_appids"], [101])
        self.assertFalse(later["facts_publish"])
        self.assertEqual(load_json(data / "store_facts.json")["games"]["101"]["checked_at"], "2026-11-19T02:00:00Z")

    def test_store_facts_publish_and_drop_a_game_excluded_meanwhile(self):
        frozen = self.prepare()
        self.assertEqual(sorted(frozen["store_facts"]["games"]), ["101"])
        _, replay = self.publish(frozen)
        self.assertEqual(replay.result["store_facts"], "written")
        self.assertIn("101", self.remote_json("data/store_facts.json")["games"])

        again = freeze(self.frontend, input_revision=self.git(self.frontend, "rev-parse", "HEAD"))
        self.git(self.other, "fetch", "origin", "main")
        self.git(self.other, "reset", "--hard", "origin/main")
        write_json(self.other / "data/excluded_appids.json", {"appids": [101]})
        self.commit(self.other, "Adult audit exclusion")
        self.git(self.other, "push", "origin", "HEAD:main")
        _, replay = self.publish(again)
        self.assertEqual(self.remote_json("data/store_facts.json")["games"], {})

    def test_newer_store_facts_on_main_win(self):
        frozen = self.prepare()
        newer = {**frozen["store_facts"], "checked_at": "2099-01-01T00:00:00Z"}
        write_json(self.other / "data/store_facts.json", newer)
        self.commit(self.other, "Newer store facts")
        self.git(self.other, "push", "origin", "HEAD:main")
        _, replay = self.publish(frozen)
        self.assertEqual(replay.result["store_facts"], "superseded")
        self.assertEqual(self.remote_json("data/store_facts.json")["checked_at"], "2099-01-01T00:00:00Z")

    def test_malformed_store_facts_are_rejected(self):
        frozen = self.prepare()
        frozen["store_facts"]["games"]["101"]["steam_deck"] = "great"
        from radar_backend.publication.media_snapshot import FrozenMediaPublication
        with self.assertRaises(RuntimeError):
            FrozenMediaPublication(frozen)


del MediaPublicationTests

if __name__ == "__main__":
    unittest.main()


class AdvanceAccessRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def access(self):
        return load_json(self.data / "store_facts.json")["games"]["123"]["advance_access"]

    def test_coming_soon_games_read_their_edition_packages(self):
        requested = []

        def fetch(appids, data_request):
            self.assertTrue(data_request["include_release"] and data_request["include_all_purchase_options"])
            return {"123": upcoming(123), "124": store_item(124, purchase_options=EDITIONS)}

        def packages(ids):
            requested.append(ids)
            return {"11": package(11), "12": package(12, RELEASE - 3 * 86400)}
        result = cli.refresh_store_facts(self.data, [123, 124], NOW, fetch, packages)
        self.assertEqual(requested, [[11, 12]])  # The released game needs none.
        self.assertEqual(self.access(), {"starts_at": "2026-12-01T17:00:00Z", "editions": ["Game Deluxe Edition"]})
        self.assertIsNone(load_json(self.data / "store_facts.json")["games"]["124"]["advance_access"])
        self.assertEqual(result["facts_advance_access_appids"], [123])
        self.assertEqual(result["facts_package_count"], 2)
        self.assertEqual(result["facts_changed_appids"], [123, 124])

    def test_unread_packages_keep_the_last_start_and_a_begun_one_stays(self):
        def fetch(appids, data_request):
            return {"123": upcoming(123)}

        def early(ids):
            return {"12": package(12, RELEASE - 3 * 86400)}

        def down(ids):
            raise RuntimeError("Steam is down")
        cli.refresh_store_facts(self.data, [123], NOW, fetch, early)
        result = cli.refresh_store_facts(self.data, [123], NOW + timedelta(hours=6), fetch, down)
        self.assertEqual(result["facts_package_failed_appids"], [123])
        self.assertEqual(self.access()["starts_at"], "2026-12-01T17:00:00Z")
        # Steam no longer lists a start still to come: it is dropped.
        cli.refresh_store_facts(self.data, [123], NOW + timedelta(hours=12), fetch, lambda ids: {})
        self.assertIsNone(self.access())
        # One that has begun stays after the full release, when Steam may drop it.
        cli.refresh_store_facts(self.data, [123], NOW + timedelta(hours=18), fetch, early)

        def released(appids, data_request):
            return {"123": store_item(123, purchase_options=EDITIONS,
                                      release={"is_coming_soon": False, "steam_release_date": RELEASE})}
        after = datetime(2026, 12, 5, tzinfo=timezone.utc)
        result = cli.refresh_store_facts(self.data, [123], after, released,
                                         lambda ids: self.fail("released games need no packages"))
        self.assertEqual(self.access()["starts_at"], "2026-12-01T17:00:00Z")
        self.assertEqual(result["facts_package_count"], 0)

    def test_early_access_start_is_kept_after_the_full_release(self):
        def in_early_access(appids, data_request):
            return {"123": store_item(123, release={"is_early_access": True, "steam_release_date": EA_START}),
                    "124": store_item(124)}

        def released(appids, data_request):
            return {"123": store_item(123, release={"steam_release_date": FULL}), "124": store_item(124)}
        first = cli.refresh_store_facts(self.data, [123, 124], NOW, in_early_access, lambda ids: {})
        self.assertEqual(first["facts_early_access_appids"], [123])
        self.assertEqual(first["facts_full_release_appids"], [])
        later = datetime(2027, 9, 4, tzinfo=timezone.utc)
        result = cli.refresh_store_facts(self.data, [123, 124], later, released, lambda ids: {})
        games = load_json(self.data / "store_facts.json")["games"]
        self.assertEqual(games["123"]["early_access"], {"active": False, "started_at": "2026-09-20T12:00:00Z",
                                                        "full_release_at": "2027-09-03T19:33:20Z"})
        self.assertIsNone(games["124"]["early_access"])
        self.assertEqual((result["facts_early_access_appids"], result["facts_full_release_appids"]), ([], [123]))
        self.assertEqual(result["facts_changed_appids"], [123])
