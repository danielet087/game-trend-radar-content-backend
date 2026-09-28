import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from enrich_game import SteamRateLimit, appdetails, fetch_store_taxonomy, fetch_taxonomy_from_api, taxonomy_fields, upsert_sharded
from reconcile_catalog import metadata_gaps
from steam_taxonomy import parse_store_taxonomy


def page(rows, appid=123, language="zh-tw", genres=True):
    detail = '''<div id="genresAndManufacturer" class="details_block">
      <b>名稱:</b> Example<br><b>類型:</b><span>
      <a href="https://store.steampowered.com/genre/Adventure/?x=1">冒險</a>
      <a href="https://store.steampowered.com/genre/RPG/">角色扮演</a>
      </span><br><div class="dev_row"><a href="https://example.test/genre/Fake/">假的</a></div></div>''' if genres else ""
    return f'<html lang="{language}">{detail}<script>InitAppTagModal( {appid}, {json.dumps(rows)}, [] );</script></html>'


ROWS = [{"tagid":7926, "name":"人工智慧", "browseable":True},
        {"tagid":4845, "name":"資本主義", "browseable":True}]
NAMES = {7926:"Artificial Intelligence", 4845:"Capitalism"}


class TaiwanTaxonomyTests(unittest.TestCase):
    def test_official_labels_keep_stable_identity_and_order(self):
        data = parse_store_taxonomy(page(ROWS + ROWS), 123, NAMES)
        self.assertEqual(data["tags"], list(NAMES.values()))
        self.assertEqual(data["tag_labels_zh_tw"]["Artificial Intelligence"], "人工智慧")
        self.assertEqual(data["tag_ids"]["Capitalism"], 4845)
        self.assertEqual(data["genres"], ["Adventure", "RPG"])
        self.assertEqual(data["genre_labels_zh_tw"], {"Adventure":"冒險", "RPG":"角色扮演"})

    def test_wrong_game_language_or_markup_cannot_be_successful_empty(self):
        for body in (page(ROWS, 999), page(ROWS, language="en"), '<html lang="zh-tw">unavailable</html>', page({"bad":"data"})):
            self.assertIsNone(parse_store_taxonomy(body, 123, NAMES))
        self.assertEqual(parse_store_taxonomy(page([]), 123, NAMES)["tags"], [])
        self.assertIsNone(parse_store_taxonomy(page(ROWS, genres=False), 123, NAMES)["genres"])

    def test_unknown_new_tag_keeps_steam_id_and_official_label(self):
        row = parse_store_taxonomy(page([{"tagid":9999,"name":"新標籤"}]), 123, NAMES)
        self.assertEqual(row["tags"], ["steam-tag:9999"])
        self.assertEqual(row["tag_labels_zh_tw"], {"steam-tag:9999":"新標籤"})

    def test_session_caches_tag_identity_lookup_and_requests_taiwan_chinese_page(self):
        calls = []
        def get(url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(status_code=200, text=page(ROWS), raise_for_status=lambda:None)
        session = SimpleNamespace(get=get)
        result = {"response":{"tags":[{"tagid":k,"name":v} for k,v in NAMES.items()]}}
        with patch("enrich_game.request_json", return_value=result) as lookup:
            self.assertEqual(fetch_store_taxonomy(session, 123)["tags"], list(NAMES.values()))
            fetch_store_taxonomy(session, 123)
        self.assertEqual(lookup.call_count, 1)
        self.assertTrue(all(args["params"] == {"cc":"TW","l":"tchinese"} for _,args in calls))

    def test_rate_limit_stops_batch_instead_of_clearing_metadata(self):
        session = SimpleNamespace(_radar_tag_names=NAMES, get=lambda *args,**kwargs:SimpleNamespace(status_code=429))
        with self.assertRaises(SteamRateLimit):
            fetch_store_taxonomy(session, 123)

    def test_appdetails_uses_taiwan_traditional_chinese(self):
        with patch("enrich_game.request_json", return_value={"123":{"success":True,"data":{"genres":[]}}}) as fetch:
            appdetails(object(), 123)
        self.assertEqual(fetch.call_args.kwargs["params"], {"appids":123,"cc":"TW","l":"tchinese"})

    def test_public_api_fallback_joins_labels_by_ids_not_locale_order(self):
        session = SimpleNamespace(_radar_tag_names_tw={7926:"人工智慧",4845:"資本主義"})
        chinese = {"genres":[{"id":"3","description":"角色扮演"},{"id":"25","description":"冒險"}]}
        english = {"genres":[{"id":"25","description":"Adventure"},{"id":"3","description":"RPG"}]}
        with patch("enrich_game.browse_one", return_value={"appid":123,"success":1,"tagids":[4845,7926]}) as browse, \
             patch("enrich_game.appdetails", side_effect=[chinese,english]):
            row = fetch_taxonomy_from_api(session,123,NAMES)
        self.assertEqual(row["tags"], ["Capitalism","Artificial Intelligence"])
        self.assertEqual(row["tag_labels_zh_tw"]["Capitalism"], "資本主義")
        self.assertEqual(row["genres"], ["RPG","Adventure"])
        self.assertEqual(row["genre_labels_zh_tw"], {"RPG":"角色扮演","Adventure":"冒險"})
        self.assertEqual(browse.call_args.args[2], "tchinese")

    def test_partial_failure_keeps_labels_and_all_public_projections(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / 'excluded_appids.json').write_text('{"appids":[]}')
            record = {"appid":123,"followers":6000,"release_start":"2030-01-01","header_image":"known",
                      "language_support":{"english":True}, **taxonomy_fields(parse_store_taxonomy(page(ROWS),123,NAMES),123)}
            upsert_sharded(data, record, record["release_start"], force=True)
            upsert_sharded(data, {**record, **taxonomy_fields(None,123)}, record["release_start"], force=True)
            current = json.loads((data/'games/123.json').read_text())
            for key in ('tags','genres','tag_labels_zh_tw','genre_labels_zh_tw','tag_ids','tags_checked_at'):
                self.assertEqual(current[key], record[key])
            self.assertEqual(current['tags_fetch_status'], 'retry')
            projection = json.loads((data/'catalog.json').read_text())['games'][0]
            self.assertEqual(projection['tag_labels_zh_tw'], record['tag_labels_zh_tw'])
            self.assertEqual(projection['genre_labels_zh_tw'], record['genre_labels_zh_tw'])
            self.assertIn('tags', metadata_gaps(current,record))
            self.assertIn('genres_zh_tw', metadata_gaps(current,record))
