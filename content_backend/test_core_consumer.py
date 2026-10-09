"""The content publisher executes the installed shared admission policy."""
from pathlib import Path
import unittest

from radar_core.domain import twitch_admission as core
import enrich_game
import public_catalog
import twitch_steam_admission as compatibility


class CoreConsumerTests(unittest.TestCase):
    def test_legacy_api_exports_the_shared_rules(self):
        expected = {
            "EVIDENCE_SOURCES", "METHOD", "TAIPEI", "TW_STORE_DATE_AUTHORITY",
            "TW_STORE_DATE_PROVIDER", "aware_time", "decimal_id",
            "has_taiwan_store_date_authority", "has_twitch_admission",
            "is_twitch_qualified", "normalize_twitch_admission",
            "preserve_twitch_admission", "resolve_store_release_day",
            "valid_enrollment", "validate_twitch_snapshot",
        }
        self.assertEqual(set(compatibility.__all__), expected)
        for name in expected:
            with self.subTest(name=name):
                self.assertIs(getattr(compatibility, name), getattr(core, name))

    def test_enrichment_and_publication_use_the_same_policy(self):
        self.assertIs(enrich_game.normalize_twitch_admission, core.normalize_twitch_admission)
        self.assertIs(public_catalog.preserve_twitch_admission, core.preserve_twitch_admission)

    def test_event_dependencies_exist_before_proof_validation(self):
        root = Path(__file__).resolve().parents[1]
        event = (root / ".github/workflows/steam-content-enrichment-dispatch.yml").read_text()
        self.assertLess(event.index("actions/setup-python@"), event.index("name: Validate qualified-game event"))
        self.assertLess(event.index("pip install"), event.index("name: Validate qualified-game event"))
        for name in ("steam-content-enrichment-dispatch.yml", "steam-catalog-reconcile.yml"):
            text = (root / ".github/workflows" / name).read_text()
            sparse = text.split("sparse-checkout: |", 1)[1].split("sparse-checkout-cone-mode:", 1)[0]
            checked_out = {line.strip() for line in sparse.splitlines() if line.strip()}
            required = {
                "requirements-core.txt",
                ".github/workflows/steam-content-enrichment-dispatch.yml",
                ".github/workflows/steam-catalog-reconcile.yml",
            }
            self.assertTrue(required <= checked_out, required - checked_out)


if __name__ == "__main__":
    unittest.main()
