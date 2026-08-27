import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from server.services import filters as filters_service  # noqa: E402


class FiltersLegacyAliasMergeTest(unittest.TestCase):
    """Regression test: a playable still tagged with a legacy logical_service
    alias (e.g. 'aiv_watch_for_free', pre-dating the aiv_free rename) showed
    up in the Filters UI as its own separate checkbox. Checking it and
    saving appeared to work, but db/preferences.py's save()/load() both
    normalize enabled_services through get_canonical_service_code() -- so
    the saved preference became 'aiv_free', not 'aiv_watch_for_free'. On
    the next page load the legacy-keyed checkbox no longer matched anything
    in enabled_services and rendered disabled again: permanently
    un-toggleable via that specific checkbox, even though the content was
    already correctly included via filter_integration.get_filtered_playables
    (which does normalize before matching).

    _build_filters() must normalize+merge before building checkbox entries
    so a legacy-tagged playable folds into its canonical service's entry
    instead of getting its own dead-end checkbox.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute(
            """
            CREATE TABLE events (
                id TEXT PRIMARY KEY,
                start_utc TEXT,
                end_utc TEXT,
                genres_json TEXT,
                classification_json TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE playables (
                event_id TEXT,
                provider TEXT,
                deeplink_play TEXT,
                deeplink_open TEXT,
                playable_url TEXT,
                service_name TEXT,
                logical_service TEXT
            )
            """
        )
        # Within the default days_ahead window (7 days) -- get_all_logical_services_with_counts()
        # caps its forward window to days_ahead, so a far-future fixture (this used to be
        # hardcoded to 2099) would be silently excluded and produce a misleading empty result.
        now = datetime.now(timezone.utc)
        start_utc = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = (now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [
            ("evt-1", "aiv", "aiv_watch_for_free"),
            ("evt-2", "aiv", "aiv_free"),
        ]
        for event_id, provider, logical_service in rows:
            self.conn.execute(
                "INSERT INTO events (id, start_utc, end_utc, genres_json, classification_json) VALUES (?, ?, ?, '[]', '[]')",
                (event_id, start_utc, end_utc),
            )
            self.conn.execute(
                """
                INSERT INTO playables (event_id, provider, deeplink_play, deeplink_open, playable_url, service_name, logical_service)
                VALUES (?, 'aiv', 'aiv://x', 'aiv://x', NULL, 'Amazon', ?)
                """,
                (event_id, logical_service),
            )
        self.conn.commit()

    def test_legacy_alias_merges_into_canonical_checkbox(self):
        result = filters_service._build_filters(self.conn)
        schemes = [s["scheme"] for s in result["amazon_services"]]
        self.assertNotIn(
            "aiv_watch_for_free", schemes,
            "legacy alias must not appear as its own checkbox",
        )
        self.assertIn("aiv_free", schemes)
        self.assertEqual(schemes.count("aiv_free"), 1, "must not produce duplicate entries")

        entry = next(s for s in result["amazon_services"] if s["scheme"] == "aiv_free")
        self.assertEqual(entry["count"], 2, "counts from both the legacy-tagged and canonical rows must merge")


if __name__ == "__main__":
    unittest.main()
