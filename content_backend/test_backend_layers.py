"""Layer boundaries and collection budgets, exercised without upstream calls."""
from __future__ import annotations
import ast
import copy
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock

import requests
from radar_backend.adapters import steam_store
from radar_backend.application.enrichment import EnrichmentPorts, build_record
from radar_backend.application.reconciliation import ReconcilePorts, reconcile
from radar_backend.domain.content import ContentSnapshot, record_from_snapshot
from radar_backend.state.json_documents import load_json, read_json, write_json
from steam_player_modes import BROWSE_SOURCE

NOW = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
CHECKED = "2026-10-08T14:00:00Z"


def source_item():
    return {
        "appid": 123, "success": 1, "name": "Example",
        "release": {"coming_soon_display": "date_full", "steam_release_date": 1792790400},
        "categories": {"supported_player_categoryids": [2, 9]},
        "supported_languages": [{"elanguage": 0, "supported": True}],
        "assets": {"asset_url_format": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/123/${FILENAME}",
                   "header": "header.jpg"},
    }


def enrichment_ports(item=None):
    item = source_item() if item is None else item
    browse = Mock(side_effect=lambda _session, _appid, language: {
        **item, "name": "官方中文名" if language == "tchinese" else "Example",
    })
    return EnrichmentPorts(
        browse=browse, details=Mock(return_value={}),
        taxonomy=Mock(return_value={"tags": [], "tag_ids": {}, "tag_labels_zh_tw": {},
                                   "genres": [], "genre_labels_zh_tw": {}}),
        describe=Mock(return_value={"short_description": "經查證的繁體中文介紹", "short_description_language": "zh-TW"}),
        convert=lambda text: text, sleep=Mock(), utc_now=lambda: CHECKED, now=lambda: NOW,
    )


class ArchitectureTests(unittest.TestCase):
    def test_inner_layers_do_not_import_legacy_entries_or_io_adapters(self):
        package = Path(__file__).with_name("radar_backend")
        for path in package.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                for module in modules:
                    with self.subTest(path=str(path.relative_to(package)), module=module):
                        self.assertNotIn(module, {"enrich_game", "reconcile_catalog"})
                        if path.parent.name in {"domain", "application"}:
                            self.assertFalse(module == "requests" or module.startswith("radar_backend.adapters"))
                            self.assertFalse(module.startswith("urllib.request"))
                if path.parent.name == "domain" and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, {"post", "read_text", "write_text", "unlink", "glob"})

    def test_enrichment_preserves_query_order_interval_and_public_fields(self):
        ports = enrichment_ports()
        record = build_record(object(), ports=ports, appid=123, followers=6000,
                              event_release_date="2026-10-23", follower_checked_at=CHECKED)
        self.assertEqual([call.args[2] for call in ports.browse.call_args_list], ["english", "tchinese", "schinese"])
        self.assertEqual([call.args for call in ports.sleep.call_args_list], [(1.0,), (1.0,)])
        ports.taxonomy.assert_called_once()
        self.assertEqual(record["display_name"], "官方中文名")
        self.assertEqual(record["short_description_language"], "zh-TW")
        self.assertEqual(record["release_start"], "2026-10-23")
        self.assertTrue(record["release_date_conflict"])
        self.assertIsNone(record["release_date_verified_at"])
        self.assertEqual(record["categories_source"], BROWSE_SOURCE)
        self.assertEqual(record["content_enrichment_version"], 5)

    def test_invalid_followers_stop_before_sources(self):
        ports = enrichment_ports()
        with self.assertRaisesRegex(RuntimeError, "Followers"):
            build_record(object(), ports=ports, appid=123, followers=4999,
                         event_release_date="2026-10-23", follower_checked_at=None)
        ports.browse.assert_not_called()
        ports.taxonomy.assert_not_called()

    def test_adult_snapshot_stops_before_taxonomy(self):
        ports = enrichment_ports({**source_item(), "content_descriptorids": [3]})
        with self.assertRaisesRegex(RuntimeError, "adult-only"):
            build_record(object(), ports=ports, appid=123, followers=6000,
                         event_release_date="2026-10-23", follower_checked_at=None)
        ports.taxonomy.assert_not_called()
        ports.describe.assert_not_called()

    def test_uncertain_store_date_remains_pending_before_description_lookup(self):
        item = {**source_item(), "release": {"coming_soon_display": "quarter"}}
        ports = enrichment_ports(item)
        with self.assertRaisesRegex(RuntimeError, "exact Store date"):
            build_record(object(), ports=ports, appid=123, followers=6000,
                         event_release_date="2026-10-23", follower_checked_at=None)
        ports.describe.assert_not_called()

    def test_snapshot_transformation_is_repeatable_and_does_not_mutate_sources(self):
        item = source_item()
        snapshot = ContentSnapshot(item, item, item, {})
        before = copy.deepcopy(snapshot)
        arguments = dict(appid=123, followers=6000, event_release_date="2026-10-23",
                         follower_checked_at=None, allow_historical=False, admission=None,
                         taxonomy={}, descriptions={}, checked_at=CHECKED, observed_now=NOW,
                         convert=lambda text: text)
        self.assertEqual(record_from_snapshot(snapshot, **arguments), record_from_snapshot(snapshot, **arguments))
        self.assertEqual(snapshot, before)

    def test_reconciliation_can_run_with_an_in_memory_state_and_publication(self):
        record = {
            "appid": 123, "followers": 6000, "release_start": "2030-01-01",
            "release_display_precision": "date_full", "header_image": "verified.jpg",
            "artwork_checked_at": CHECKED, "language_support": {"english": True},
            "tags": [], "tags_fetch_status": "ok", "tag_labels_language": "zh-TW",
            "genres_fetch_status": "ok", "genre_labels_language": "zh-TW",
            "description_checked_at": CHECKED, "short_description_language": "zh-TW",
            "categories": [], "categories_source": BROWSE_SOURCE, "categories_checked_at": CHECKED,
        }
        files = {
            Path("virtual/master.json"): {"games": [record]},
            Path("virtual/data/games/123.json"): record,
            Path("virtual/data/index.json"): {"game_count": 1},
        }
        write = Mock(side_effect=lambda path, value: files.update({path: value}))
        ports = ReconcilePorts(
            read_json=lambda path, default=None: files.get(path, default), write_json=write,
            excluded=lambda _path: set(), build_record=Mock(side_effect=AssertionError("already complete")),
            refresh_categories=Mock(side_effect=AssertionError("already complete")),
            upsert=Mock(), in_sync=lambda _path, _row: True, rebuild=Mock(),
            session_factory=lambda: types.SimpleNamespace(headers={}), monotonic=lambda: 0,
            now=lambda _tz: NOW, utc_now=lambda: CHECKED,
            transport_error=requests.RequestException, rate_limit=steam_store.SteamRateLimit,
        )
        result = reconcile(Path("virtual/master.json"), Path("virtual/data"), 60, ports=ports)
        self.assertTrue(result["complete"])
        self.assertEqual(result["attempted"], 0)
        self.assertEqual(result["public_count"], 1)
        self.assertEqual(files[Path("virtual/data/content_refresh_status.json")], result)
        ports.build_record.assert_not_called()
        ports.rebuild.assert_called_once()

    def test_legacy_and_new_cli_help_do_not_start_collection(self):
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ, PYTHONPATH=str(root / "content_backend"), PYTHONDONTWRITEBYTECODE="1")
        commands = [
            ["content_backend/enrich_game.py"], ["content_backend/reconcile_catalog.py"],
            ["-m", "content_backend.enrich_game"], ["-m", "content_backend.reconcile_catalog"],
            ["-m", "radar_backend.jobs.enrich_content"], ["-m", "radar_backend.jobs.reconcile_content"],
        ]
        for command in commands:
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, *command, "--help"], cwd=root, env=environment,
                                        text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)
        # Root-module compatibility also works without a caller-provided PYTHONPATH.
        environment.pop("PYTHONPATH")
        result = subprocess.run([sys.executable, "-m", "content_backend.enrich_game", "--help"],
                                cwd=root, env=environment, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class StorageAndTransportTests(unittest.TestCase):
    def test_429_is_not_retried_inside_the_current_batch(self):
        response = Mock(status_code=429)
        session = Mock()
        session.get.return_value = response
        sleep = Mock()
        with self.assertRaises(steam_store.SteamRateLimit):
            steam_store.request_json(session, "https://invalid.local", params={}, sleep=sleep)
        session.get.assert_called_once_with("https://invalid.local", params={}, timeout=35)
        sleep.assert_not_called()

    def test_transient_retry_budget_remains_four_attempts(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True}
        session = Mock()
        session.get.side_effect = [requests.Timeout(), requests.Timeout(), requests.Timeout(), response]
        sleep = Mock()
        self.assertEqual(steam_store.request_json(session, "https://invalid.local", params={}, sleep=sleep), {"ok": True})
        self.assertEqual(session.get.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [4, 8, 12])

    def test_appdetails_best_effort_budget_remains_three_outer_attempts(self):
        request = Mock(return_value={"123": {"success": False}})
        sleep = Mock()
        self.assertEqual(steam_store.appdetails(object(), 123, request=request, sleep=sleep), {})
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(call.kwargs["attempts"] == 2 for call in request.call_args_list))
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 10])

    def test_state_preserves_strict_and_legacy_tolerant_read_contracts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nested/state.json"
            write_json(path, {"games": []})
            self.assertEqual(load_json(path), {"games": []})
            path.write_text("broken JSON")
            with self.assertRaises(ValueError):
                load_json(path)
            self.assertEqual(read_json(path, {"legacy": True}), {"legacy": True})


if __name__ == "__main__":
    unittest.main()
