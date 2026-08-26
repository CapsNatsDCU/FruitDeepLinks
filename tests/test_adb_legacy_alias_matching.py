import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from fruit_build_adb_lanes import load_events_for_provider  # noqa: E402


class AdbLegacyAliasMatchingTest(unittest.TestCase):
    """Regression test: load_events_for_provider() builds its SQL WHERE
    clause by comparing playables.logical_service directly against
    enabled_services (already-canonical, per db/preferences.py's save/load
    normalization). A playable still tagged with a legacy alias (e.g.
    'aiv_watch_for_free', pre-dating the aiv_free rename) never matched
    that raw comparison, so it silently dropped out of ADB lane building
    even though it's correctly included by filter_integration's
    get_filtered_playables() (which does normalize). expand_with_legacy_aliases()
    closes that gap by also including legacy alias codes in the SQL
    candidate list.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute(
            """
            CREATE TABLE events (
                id TEXT PRIMARY KEY,
                title TEXT,
                start_utc TEXT,
                end_utc TEXT,
                start_ms INTEGER,
                end_ms INTEGER,
                classification_json TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE playables (
                event_id TEXT,
                provider TEXT,
                logical_service TEXT,
                playable_id TEXT,
                service_name TEXT,
                locale TEXT,
                priority INTEGER,
                locale_fallback INTEGER DEFAULT 0
            )
            """
        )

    def _insert_event(self, event_id, logical_service):
        self.conn.execute(
            "INSERT INTO events (id, title, start_utc, end_utc) VALUES (?, ?, '2026-01-01T00:00:00Z', '2026-01-01T02:00:00Z')",
            (event_id, "Test Event"),
        )
        self.conn.execute(
            "INSERT INTO playables (event_id, provider, logical_service, playable_id, priority) VALUES (?, 'aiv', ?, ?, 10)",
            (event_id, logical_service, f"{event_id}-p1"),
        )
        self.conn.commit()

    def test_amazon_branch_matches_legacy_tagged_playable(self):
        self._insert_event("evt-legacy", "aiv_watch_for_free")
        results = load_events_for_provider(
            self.conn, "aiv", enabled_services=["aiv_free"],
        )
        self.assertEqual(len(results), 1, "legacy-tagged playable must still be found via its canonical alias")
        self.assertEqual(results[0]["id"], "evt-legacy")

    def test_amazon_branch_still_matches_canonical_tagged_playable(self):
        self._insert_event("evt-canonical", "aiv_free")
        results = load_events_for_provider(
            self.conn, "aiv", enabled_services=["aiv_free"],
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "evt-canonical")

    def test_amazon_branch_excludes_unrelated_service(self):
        self._insert_event("evt-other", "aiv_prime")
        results = load_events_for_provider(
            self.conn, "aiv", enabled_services=["aiv_free"],
        )
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
