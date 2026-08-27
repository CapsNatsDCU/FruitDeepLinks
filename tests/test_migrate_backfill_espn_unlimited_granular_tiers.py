import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import migrate_backfill_espn_unlimited_granular_tiers as migrate  # noqa: E402


class MigrateBackfillEspnUnlimitedGranularTiersTest(unittest.TestCase):
    """Regression coverage for the one-time backfill that replaced the live
    espn_unlimited -> espn_mlb_tv/espn_mlb_network wildcard (removed from
    filter_integration.get_filtered_playables() and fruit_build_adb_lanes.py
    because it silently overrode an explicit uncheck of ESPN MLB.TV --
    reported on the forum).
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT)"
        )

    def _set_enabled_services(self, services):
        self.conn.execute(
            "INSERT OR REPLACE INTO user_preferences (key, value) VALUES ('enabled_services', ?)",
            (json.dumps(services),),
        )
        self.conn.commit()

    def _get_enabled_services(self):
        row = self.conn.execute(
            "SELECT value FROM user_preferences WHERE key = 'enabled_services'"
        ).fetchone()
        return json.loads(row[0]) if row else []

    def test_backfills_granular_tiers_for_bare_espn_unlimited(self):
        self._set_enabled_services(["espn_unlimited", "peacock_web"])
        changed = migrate.backfill(self.conn)
        self.assertTrue(changed)
        result = self._get_enabled_services()
        self.assertIn("espn_mlb_tv", result)
        self.assertIn("espn_mlb_network", result)
        self.assertIn("peacock_web", result)

    def test_does_not_touch_prefs_with_explicit_granular_tier(self):
        self._set_enabled_services(["espn_unlimited", "espn_mlb_tv"])
        migrate.backfill(self.conn)
        result = self._get_enabled_services()
        self.assertEqual(sorted(result), ["espn_mlb_tv", "espn_unlimited"])

    def test_does_not_touch_empty_enabled_services(self):
        # Empty enabled_services already means "allow everything" -- nothing to backfill.
        self._set_enabled_services([])
        migrate.backfill(self.conn)
        self.assertEqual(self._get_enabled_services(), [])

    def test_idempotent_second_run_is_a_noop(self):
        self._set_enabled_services(["espn_unlimited"])
        migrate.backfill(self.conn)
        after_first = self._get_enabled_services()

        # Simulate the user explicitly unchecking espn_mlb_tv again after the
        # one-time backfill -- a second run must NOT re-add it.
        self._set_enabled_services([s for s in after_first if s != "espn_mlb_tv"])
        changed = migrate.backfill(self.conn)
        self.assertFalse(changed, "marker must prevent re-running and re-overriding the explicit uncheck")
        self.assertNotIn("espn_mlb_tv", self._get_enabled_services())

    def test_marker_is_set_after_running(self):
        self._set_enabled_services(["espn_unlimited"])
        migrate.backfill(self.conn)
        row = self.conn.execute(
            "SELECT value FROM user_preferences WHERE key = ?", (migrate._MARKER_KEY,)
        ).fetchone()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
