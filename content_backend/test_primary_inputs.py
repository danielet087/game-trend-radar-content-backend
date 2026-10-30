import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from radar_backend.jobs.prepare_state import prepare, FRONTEND, BACKEND, MASTER
from radar_backend.jobs.validate_content_event import validate_event, EVIDENCE_PATHS
from radar_backend.jobs.publish_content import _revision
from test_twitch_steam_admission import proof


def evidence(admission):
    enrollment = admission["source_enrollment"]
    return ({"schema_version": 1, "games": {"456": {"game_id": "456", "igdb_id": "789",
                "tracking_sources": {"twitch_new": {"source": "twitch_new", "status": "active",
                    "enrollment": enrollment}}}}},
            {"schema_version": 1, "steam_source_id": "1", "games": {"456": {
                "status": "matched", "active": True, "method": admission["method"],
                "twitch_game_id": "456", "igdb_id": "789", "twitch_enrollment": enrollment,
                "checked_at": admission["checked_at"], "steam_appids": [123],
                "links": [{"steam_appid": 123, "uid": "123", "external_game_id": "333",
                           "external_game_source": 1, "game": "789"}]}}})


class PrimaryInputTests(unittest.TestCase):
    def test_primary_hydrates_clean_baseline_and_authoritative_master(self):
        store = Mock()
        store.hydrate.return_value = {"revision": "database-front", "generation": 8}
        raw = b'{"games":[{"appid":12}],"extra":"preserved"}'
        store.read_files.return_value = ({"revision": "database-master"}, {MASTER: raw})
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"RADAR_DATABASE_PRIMARY": "1"}):
            base = Path(folder)
            result = prepare(base / "frontend", backend=base / "backend", storage=store)
            self.assertEqual((base / "backend" / MASTER).read_bytes(), raw)
            self.assertEqual(result["master_revision"], "database-master")
            store.hydrate.assert_called_once_with(base / "frontend", FRONTEND, clean_baseline=True)
            store.read_files.assert_called_once_with(BACKEND, [MASTER])

    def test_primary_failure_never_uses_git_as_fallback(self):
        store = Mock()
        store.hydrate.side_effect = RuntimeError("database unavailable")
        with patch.dict(os.environ, {"RADAR_DATABASE_PRIMARY": "1"}), \
             patch("radar_backend.jobs.prepare_state.subprocess.run") as git:
            with self.assertRaises(RuntimeError):
                prepare(Path("frontend"), backend=Path("backend"), storage=store)
            git.assert_not_called()

    def test_source_revision_is_database_input_and_missing_hydration_fails(self):
        with patch.dict(os.environ, {"RADAR_DATABASE_PRIMARY": "1", "RADAR_INPUT_REVISION": "database-revision"}):
            self.assertEqual(_revision(Path("missing")), "database-revision")
        with patch.dict(os.environ, {"RADAR_DATABASE_PRIMARY": "1", "RADAR_INPUT_REVISION": ""}):
            with self.assertRaises(RuntimeError):
                _revision(Path("missing"))

    def test_missing_followers_event_still_requires_immutable_twitch_proof(self):
        admission = proof()
        registry, discovery = evidence(admission)
        store = Mock()
        store.read_files.return_value = ({}, {path: json.dumps(value).encode()
            for path, value in zip(EVIDENCE_PATHS, (registry, discovery))})
        env = {"RADAR_DATABASE_PRIMARY": "1", "APPID": "123", "OFFICIAL_FOLLOWERS": "",
               "RELEASE_DATE": "2030-01-01", "TWITCH_ADMISSION_JSON": json.dumps(admission),
               "EVENT_ACTION": "steam_game_twitch_discovered"}
        self.assertEqual(validate_event(env, storage=store), admission)
        env['TWITCH_ADMISSION_JSON'] = 'null'
        with self.assertRaises(ValueError):
            validate_event(env, storage=store)

    def test_legacy_commit_proof_uses_exact_immutable_database_evidence(self):
        admission = proof()
        registry, discovery = evidence(admission)
        store = Mock()
        store.read_files.return_value = ({}, {path: json.dumps(value).encode()
            for path, value in zip(EVIDENCE_PATHS, (registry, discovery))})
        env = {"RADAR_DATABASE_PRIMARY": "1", "APPID": "123", "OFFICIAL_FOLLOWERS": "240",
               "RELEASE_DATE": "2030-01-01", "TWITCH_ADMISSION_JSON": json.dumps(admission),
               "EVENT_ACTION": "steam_game_twitch_discovered"}
        opener = Mock(side_effect=AssertionError("Git fallback"))
        self.assertEqual(validate_event(env, storage=store, opener=opener), admission)
        store.read_files.assert_called_once_with(FRONTEND, EVIDENCE_PATHS, revision="a" * 40)
        opener.assert_not_called()
        discovery["games"]["456"]["steam_appids"] = [999]
        store.read_files.return_value[1][EVIDENCE_PATHS[1]] = json.dumps(discovery).encode()
        with self.assertRaises(ValueError):
            validate_event(env, storage=store, opener=opener)

    def test_missing_immutable_proof_evidence_fails_without_network_fallback(self):
        store = Mock()
        store.read_files.side_effect = RuntimeError("Unknown immutable revision")
        opener = Mock(side_effect=AssertionError("Git fallback"))
        env = {"RADAR_DATABASE_PRIMARY": "1", "APPID": "123", "OFFICIAL_FOLLOWERS": "240",
               "RELEASE_DATE": "2030-01-01", "TWITCH_ADMISSION_JSON": json.dumps(proof())}
        with self.assertRaises(RuntimeError):
            validate_event(env, storage=store, opener=opener)
        opener.assert_not_called()

    def test_database_proof_checks_manifest_generation_and_actual_enrollment(self):
        admission = proof()
        del admission["source_frontend_commit"]
        admission.update(source_database_revision="b" * 64, source_database_generation=17,
                         source_database_namespace=FRONTEND + "@main")
        registry, discovery = evidence(admission)
        store = Mock()
        head = {"manifest_digest": "b" * 64, "generation": 17}
        store.read_files.return_value = (head, {path: json.dumps(value).encode()
            for path, value in zip(EVIDENCE_PATHS, (registry, discovery))})
        env = {"RADAR_DATABASE_PRIMARY": "1", "APPID": "123", "OFFICIAL_FOLLOWERS": "240",
               "RELEASE_DATE": "2030-01-01", "TWITCH_ADMISSION_JSON": json.dumps(admission)}
        opener = Mock(side_effect=AssertionError("Git fallback"))
        self.assertEqual(validate_event(env, storage=store, opener=opener), admission)
        store.read_files.assert_called_once_with(FRONTEND, EVIDENCE_PATHS, revision="b" * 64)
        head["generation"] = 18
        with self.assertRaises(ValueError):
            validate_event(env, storage=store, opener=opener)
        head["generation"] = 17
        registry["games"]["456"]["tracking_sources"]["twitch_new"]["enrollment"]["viewer_count"] = 7001
        store.read_files.return_value[1][EVIDENCE_PATHS[0]] = json.dumps(registry).encode()
        with self.assertRaises(ValueError):
            validate_event(env, storage=store, opener=opener)
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
