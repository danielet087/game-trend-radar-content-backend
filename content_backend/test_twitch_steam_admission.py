"""Twitch discoveries keep an explicit, validated admission through publication."""
import copy
import io
import os
import re
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from enrich_game import build_record, upsert_sharded, _shard_in_sync
from public_catalog import keep_newer_release
from reconcile_catalog import reconcile, metadata_gaps
from twitch_steam_admission import has_twitch_admission, is_twitch_qualified, normalize_twitch_admission, validate_twitch_snapshot


def proof(appid=123):
    return {'schema_version': 1, 'method': 'twitch_igdb_external_steam_v1', 'appid': appid,
            'twitch_game_id': '456', 'igdb_id': '789', 'checked_at': '2026-10-02T08:00:00Z',
            'source_frontend_commit': 'a' * 40,
            'source_enrollment': {'source': 'igdb_first_release_date', 'observed_at': '2026-10-01T00:00:00Z',
                                  'viewer_count': 8000, 'min_viewers': 7000}}


def row(day='2030-01-01', followers=240):
    stamp = day + 'T00:00:00Z'
    return {'appid': 123, 'followers': followers, 'follower_checked_at': '2026-10-02T08:00:00Z',
            'release_start': day, 'release_end': day, 'release_precision': 'day',
            'release_display_precision': 'date_full', 'release_date_timezone': 'Asia/Taipei',
            'release_time_utc': stamp, 'release_timestamp_taipei_date': day, 'release_date_conflict': False,
            'steam_type': 'game', 'sexual_content_screened': True, 'twitch_admission': proof(),
            'header_image': 'known', 'artwork_checked_at': 'checked', 'description_checked_at': 'checked',
            'tag_labels_language': 'zh-TW', 'genre_labels_language': 'zh-TW', 'language_support': {'english': True},
            'tags': ['Action'], 'tags_fetch_status': 'ok', 'genres': ['Action'], 'genres_fetch_status': 'ok'}


def mocks(day='2030-01-01', **changes):
    base = {'appid': 123, 'success': 1, 'visible': True, 'name': 'Example', 'release': {'coming_soon_display': 'date_full',
            'steam_release_date': int(datetime.fromisoformat(day + 'T00:00:00+00:00').timestamp())},
            'supported_languages': [{'elanguage': 0, 'supported': True}],
            'assets': {'asset_url_format': 'steam/apps/123/${FILENAME}', 'header': 'header.jpg'},
            'content_descriptorids': [], 'basic_info': {'short_description': 'An action game.'}}
    base.update(changes)
    return base


def build(base=None, details=None, **changes):
    args = dict(appid=123, followers=240, event_release_date='2030-01-01',
                follower_checked_at='2026-10-02T08:00:00Z', twitch_admission=proof())
    args.update(changes)
    with patch('enrich_game.browse_one', return_value=base or mocks()), \
         patch('enrich_game.appdetails', return_value=details if details is not None else {'type': 'game', 'steam_appid': 123, 'content_descriptors': {'ids': []}}), \
         patch('enrich_game.fetch_store_taxonomy', return_value={'tags': [], 'tag_ids': {}, 'tag_labels_zh_tw': {}, 'genres': [], 'genre_labels_zh_tw': {}}), \
         patch('enrich_game.time.sleep'):
        return build_record(object(), **args)


class TwitchAdmissionTests(unittest.TestCase):
    def test_identity_and_positive_enrollment_required(self):
        self.assertIsNotNone(normalize_twitch_admission(proof(), 123))
        for bad in [dict(proof(), appid=999), dict(proof(), source_frontend_commit='main'),
                    dict(proof(), method='twitch'), dict(proof(), checked_at='2026-10-02'),
                    dict(proof(), twitch_game_id=True)]:
            self.assertIsNone(normalize_twitch_admission(bad, 123))
        for changes in [{'viewer_count': 6999}, {'min_viewers': 0}, {'source': 'popular'},
                        {'qualification': 'unverified'}, {'viewer_count': '8000'}]:
            bad = proof()
            bad['source_enrollment'].update(changes)
            self.assertIsNone(normalize_twitch_admission(bad, 123))

    def test_source_string_cannot_replace_proof_or_steam_validation(self):
        source = row()
        self.assertTrue(is_twitch_qualified(source))
        for changes in [{'twitch_admission': 'twitch'}, {'steam_type': 'dlc'}, {'sexual_content_screened': False},
                        {'release_display_precision': 'month'}, {'release_end': '2030-01-02'},
                        {'release_time_utc': None}, {'release_date_conflict': True},
                        {'follower_checked_at': None}, {'followers': False}]:
            self.assertFalse(is_twitch_qualified(dict(source, **changes)))

    def test_low_count_has_real_count_and_does_not_claim_5000(self):
        record = build(followers=8)
        self.assertEqual(record['followers'], 8)
        self.assertFalse(record['official_ge5000'])
        self.assertTrue(is_twitch_qualified(record))
        self.assertEqual(record['twitch_admission'], proof())
        self.assertEqual(build(followers=0)['followers'], 0)

    def test_low_count_without_verified_proof_still_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'low counts require'):
            build(twitch_admission=None)
        with self.assertRaisesRegex(RuntimeError, 'Invalid Twitch'):
            build(twitch_admission={'source': 'twitch'})
        with self.assertRaisesRegex(RuntimeError, 'must be verified'):
            build(followers=-1)

    def test_steam_game_type_must_be_confirmed(self):
        for details in [{}, {'type': 'dlc'}, {'type': 'demo'}, {'type': 'software'}]:
            with self.assertRaisesRegex(RuntimeError, 'verified Steam game'):
                build(details=details)

    def test_appdetails_must_match_the_verified_appid(self):
        for details in [{'type': 'game', 'steam_appid': 999}, {'type': 'game'}]:
            with self.assertRaisesRegex(RuntimeError, 'identity does not match'):
                build(details=details)

    def test_taiwan_store_must_be_available(self):
        for changes in [{'success': 2}, {'visible': False}]:
            with self.assertRaisesRegex(RuntimeError, 'unavailable in the Taiwan Store'):
                build(base=mocks(**changes))

    def test_formal_sexual_content_rule_not_single_tag(self):
        with self.assertRaisesRegex(RuntimeError, 'adult-only'):
            build(details={'type': 'game', 'steam_appid': 123, 'content_descriptors': {'ids': [3]}})
        suspect = mocks(tags=[{'tagid': 12095}, {'tagid': 9130}], basic_info={'short_description': 'An explicit sexual game.'})
        with self.assertRaisesRegex(RuntimeError, 'formal sexual-content'):
            build(base=suspect)
        romance = mocks(tags=[{'tagid': 12095}, {'tagid': 9130}], basic_info={'short_description': 'A romantic adventure.'})
        self.assertTrue(is_twitch_qualified(build(base=romance)))
        with self.assertRaisesRegex(RuntimeError, 'formal sexual-content'):
            build(base=mocks(tagids=[12095, 9130], basic_info={'short_description': 'An explicit sexual game.'}))
        self.assertTrue(is_twitch_qualified(build(base=mocks(tags=[{'tagid': 9130}]))))

    def test_conflicting_or_absent_timestamp_blocks_new_admission(self):
        for release in [{'coming_soon_display': 'date_full'}, {'coming_soon_display': 'date_full', 'steam_release_date': 1791158400}]:
            with self.assertRaisesRegex(RuntimeError, 'timestamp does not agree'):
                build(base=mocks(release=release))

    def test_first_released_game_needs_actual_release_and_exact_store_day(self):
        day = '2026-09-29'
        base = mocks(day, release={'steam_release_date': int(datetime.fromisoformat(day + 'T00:00:00+00:00').timestamp()), 'is_coming_soon': False})
        details = {'type': 'game', 'steam_appid': 123, 'content_descriptors': {'ids': []}, 'release_date': {'date': '2026 年 9 月 29 日', 'coming_soon': False}}
        record = build(base=base, details=details, event_release_date=day)
        self.assertTrue(is_twitch_qualified(record))
        self.assertEqual(record['release_display_precision'], 'date_full')
        self.assertIsNotNone(record['release_date_verified_at'])
        for bad in [{'date': 'September 2026'}, {'date': '2026 年 9 月 28 日'}, {'date': day, 'coming_soon': True}]:
            with self.assertRaisesRegex(RuntimeError, 'matching exact'):
                build(base=base, details={**details, 'release_date': bad}, event_release_date=day)

    def test_released_today_can_be_imported_without_an_existing_public_record(self):
        day = '2026-10-02'
        base = mocks(day, release={'steam_release_date': int(datetime.fromisoformat(day + 'T00:00:00+00:00').timestamp()), 'is_coming_soon': False})
        details = {'type': 'game', 'steam_appid': 123, 'content_descriptors': {'ids': []}, 'release_date': {'date': day, 'coming_soon': False}}
        self.assertTrue(is_twitch_qualified(build(base=base, details=details, event_release_date=day)))

    def test_low_count_reaches_shards_calendar_lists_and_projection(self):
        for day in ['2030-01-01', (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()]:
            with self.subTest(day=day), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'excluded_appids.json').write_text('{"appids": []}')
                record = row(day)
                self.assertTrue(upsert_sharded(root, record, day))
                stored = json.loads((root / 'games/123.json').read_text())
                self.assertTrue(_shard_in_sync(root, stored))
                self.assertFalse(upsert_sharded(root, stored, day))
                catalog = json.loads((root / 'catalog.json').read_text())
                self.assertEqual(catalog['count'], 1)
                self.assertTrue(is_twitch_qualified(catalog['games'][0]))
                list_name = 'upcoming' if day >= datetime.now(timezone.utc).date().isoformat() else 'released'
                self.assertIn(123, json.loads((root / f'lists/{list_name}.json').read_text())['appids'])

    def test_bad_proof_cannot_write_a_public_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'excluded_appids.json').write_text('{"appids": []}')
            bad = dict(row(), twitch_admission={'source': 'twitch'})
            with self.assertRaisesRegex(RuntimeError, 'invalid Twitch-source'):
                upsert_sharded(root, bad, bad['release_start'])
            self.assertFalse((root / 'games/123.json').exists())

    def test_normal_refresh_preserves_independent_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'excluded_appids.json').write_text('{"appids": []}')
            original = row()
            upsert_sharded(root, original, original['release_start'])
            incoming = dict(original, tags=['Adventure'], followers=200)
            incoming.pop('twitch_admission')
            incoming.pop('steam_type')
            upsert_sharded(root, incoming, incoming['release_start'], force=True)
            stored = json.loads((root / 'games/123.json').read_text())
            self.assertEqual(stored['followers'], 200)
            self.assertEqual(stored['twitch_admission'], proof())
            self.assertTrue(is_twitch_qualified(stored))

    def test_proof_freshness_compares_instants_in_different_offsets(self):
        original = row()
        original['twitch_admission']['checked_at'] = '2026-10-02T16:30:00+08:00'
        incoming = row()
        incoming['twitch_admission']['checked_at'] = '2026-10-02T09:00:00Z'
        self.assertEqual(keep_newer_release(original, incoming)['twitch_admission']['checked_at'], '2026-10-02T09:00:00Z')
        self.assertEqual(keep_newer_release(incoming, original)['twitch_admission']['checked_at'], '2026-10-02T09:00:00Z')

    def test_reconcile_imports_low_followers_and_retains_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'data'
            data.mkdir()
            (data / 'excluded_appids.json').write_text('{"appids": []}')
            source = row()
            master = root / 'master.json'
            master.write_text(json.dumps({'games': [source, dict(row(), appid=999, twitch_admission=None)]}))
            with patch('reconcile_catalog.build_record', return_value=source) as enrich:
                status = reconcile(master, data, 10)
            self.assertEqual(status['qualified_master'], 1)
            self.assertEqual(status['public_count'], 1)
            self.assertEqual(enrich.call_args.kwargs['twitch_admission'], proof())
            self.assertTrue(is_twitch_qualified(json.loads((data / 'games/123.json').read_text())))

    def test_old_cache_cannot_hide_new_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'data'
            data.mkdir()
            (data / 'excluded_appids.json').write_text('{"appids": []}')
            source = row(followers=6000)
            master = root / 'master.json'
            master.write_text(json.dumps({'games': [source]}))
            cache = root / 'cache'
            cache.mkdir()
            old = dict(source)
            old.pop('twitch_admission')
            (cache / '123.json').write_text(json.dumps({'signature': '123:6000:2030-01-01', 'record': dict(old, content_enrichment_version=4)}))
            with patch('reconcile_catalog.build_record', return_value=source) as enrich:
                reconcile(master, data, 10, cache_dir=cache)
            enrich.assert_called_once()
            self.assertEqual(metadata_gaps(old, source), ['twitch_admission'])


class DispatchContractTests(unittest.TestCase):
    @staticmethod
    def event_script():
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/steam-content-enrichment-dispatch.yml').read_text()
        match = re.search(r"python - <<'PY'\n(.*?)          PY", workflow, flags=re.S)
        return '\n'.join(line[10:] for line in match[1].splitlines())

    @staticmethod
    def snapshots():
        admission = proof()
        enrollment = admission['source_enrollment']
        registry = {'schema_version': 1, 'games': {'456': {'game_id': '456', 'igdb_id': '789',
                    'tracking_sources': {'twitch_new': {'source': 'twitch_new', 'status': 'active',
                    'expires_at': '2026-11-01T00:00:00Z', 'enrollment': enrollment}}}}}
        discovery = {'schema_version': 1, 'steam_source_id': '42', 'games': {'456': {
                     'status': 'matched', 'active': True, 'method': admission['method'], 'twitch_game_id': '456',
                     'igdb_id': '789', 'twitch_enrollment': enrollment, 'checked_at': admission['checked_at'],
                     'steam_appids': ['123'], 'links': [{'steam_appid': '123', 'uid': '123',
                     'external_game_id': '999', 'external_game_source': '42', 'game': '789'}]}}}
        return registry, discovery

    def test_event_validates_same_commit_and_enrollment_before_low_count(self):
        registry, discovery = self.snapshots()
        self.assertTrue(validate_twitch_snapshot(proof(), registry, discovery))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'proof.json'
            env = {'APPID': '123', 'OFFICIAL_FOLLOWERS': '0', 'RELEASE_DATE': '2030-01-01',
                   'TWITCH_ADMISSION_JSON': json.dumps(proof()), 'EVENT_ACTION': 'steam_game_twitch_discovered',
                   'TWITCH_ADMISSION_PATH': str(dest)}
            responses = [io.BytesIO(json.dumps(x).encode()) for x in (registry, discovery)]
            with patch.dict(os.environ, env), patch('urllib.request.urlopen', side_effect=responses) as fetch:
                exec(compile(self.event_script(), '<workflow event>', 'exec'), {})
            self.assertEqual(json.loads(dest.read_text()), proof())
            self.assertTrue(all('/' + ('a' * 40) + '/data/' in call.args[0] for call in fetch.call_args_list))

    def test_refresh_preserves_proof_and_ordinary_gate_remains_5000(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'APPID': '123', 'OFFICIAL_FOLLOWERS': '4999', 'RELEASE_DATE': '2030-01-01',
                   'TWITCH_ADMISSION_JSON': 'null', 'EVENT_ACTION': 'steam_game_refresh',
                   'TWITCH_ADMISSION_PATH': str(Path(tmp) / 'proof.json')}
            with patch.dict(os.environ, env), patch('urllib.request.urlopen') as fetch:
                with self.assertRaises(AssertionError):
                    exec(compile(self.event_script(), '<workflow event>', 'exec'), {})
                fetch.assert_not_called()
            registry, discovery = self.snapshots()
            env['TWITCH_ADMISSION_JSON'] = json.dumps(proof())
            with patch.dict(os.environ, env), patch('urllib.request.urlopen', side_effect=[io.BytesIO(json.dumps(x).encode()) for x in (registry, discovery)]):
                exec(compile(self.event_script(), '<workflow event>', 'exec'), {})

    def test_wrong_source_link_or_enrollment_rejected(self):
        registry, discovery = self.snapshots()
        altered = copy.deepcopy(discovery)
        altered['games']['456']['links'][0]['external_game_source'] = '1'
        self.assertFalse(validate_twitch_snapshot(proof(), registry, altered))
        registry['games']['456']['tracking_sources']['twitch_new']['enrollment'] = dict(proof()['source_enrollment'], viewer_count=7999)
        self.assertFalse(validate_twitch_snapshot(proof(), registry, discovery))


if __name__ == '__main__':
    unittest.main()
