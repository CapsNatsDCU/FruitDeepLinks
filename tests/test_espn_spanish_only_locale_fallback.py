import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import filter_integration  # noqa: E402
import fix_espn_spanish_only  # noqa: E402


class EspnSpanishOnlyLocaleFallbackTest(unittest.TestCase):
    """Regression test: an ESPN Unlimited event where Apple TV only exposes a
    Spanish playable was left with zero valid links under an English filter.

    fix_espn_spanish_only.py rewrites the deeplink to the general broadcast
    (espn_graph_id/externalId) but the playables.locale column is left as
    'es_MX' on purpose, so it can re-detect and re-fix the row every day
    after Apple's re-scrape resets deeplink_play. Before locale_fallback
    existed, get_filtered_playables() had no way to tell "still genuinely
    Spanish-only" apart from "already repaired but still labeled es_MX", so
    it excluded the repaired playable under language_preference="en" -- the
    event's only playable -- leaving nothing for the filter/inspector to show
    even though the deeplink itself was already fixed.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute(
            """
            CREATE TABLE events (
                id TEXT PRIMARY KEY,
                raw_attributes_json TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE playables (
                event_id TEXT,
                playable_id TEXT,
                provider TEXT,
                deeplink_play TEXT,
                deeplink_open TEXT,
                playable_url TEXT,
                title TEXT,
                content_id TEXT,
                priority INTEGER,
                http_deeplink_url TEXT,
                espn_graph_id TEXT,
                service_name TEXT,
                logical_service TEXT,
                locale TEXT,
                locale_fallback INTEGER DEFAULT 0
            )
            """
        )

        raw_json = json.dumps({
            "playables": {
                "p1": {"externalId": "ext-cubs-brewers-123"},
            }
        })
        self.conn.execute(
            "INSERT INTO events (id, raw_attributes_json) VALUES ('evt-cubs', ?)",
            (raw_json,),
        )
        # The only playable Apple TV offers for this event is the Spanish one.
        self.conn.execute(
            """
            INSERT INTO playables (
                event_id, playable_id, provider, deeplink_play, deeplink_open,
                playable_url, title, content_id, priority, service_name,
                logical_service, locale
            ) VALUES (
                'evt-cubs', 'p1', 'sportscenter',
                'sportscenter://x-callback-url/showWatchStream?playID=es-only-playid',
                'sportscenter://x-callback-url/showWatchStream?playID=es-only-playid',
                'https://plus.espn.com/watch/es-only-playid',
                'Cubs vs Brewers En Español', 'p1', 26, 'ESPN Unlimited',
                'espn_unlimited', 'es_MX'
            )
            """
        )
        self.conn.commit()

    def test_english_filter_keeps_repaired_spanish_only_playable(self):
        # Before the fix: get_filtered_playables(language_preference="en")
        # returns [] for this event, matching the reported bug.
        before = filter_integration.get_filtered_playables(
            self.conn, "evt-cubs", enabled_services=[], language_preference="en",
        )
        self.assertEqual(before, [], "sanity check: unrepaired Spanish-only playable is excluded")

        candidates = fix_espn_spanish_only.find_spanish_only_events(self.conn)
        self.assertEqual(len(candidates), 1)
        updated = fix_espn_spanish_only.fix_spanish_only_playables(self.conn, candidates)
        self.assertEqual(updated, 1)

        row = self.conn.execute(
            "SELECT deeplink_play, locale, locale_fallback FROM playables WHERE event_id = 'evt-cubs'"
        ).fetchone()
        self.assertIn("ext-cubs-brewers-123", row["deeplink_play"])
        self.assertEqual(row["locale"], "es_MX", "locale must stay es_MX so it's re-detected on the next run")
        self.assertEqual(row["locale_fallback"], 1)

        after = filter_integration.get_filtered_playables(
            self.conn, "evt-cubs", enabled_services=[], language_preference="en",
        )
        self.assertEqual(len(after), 1, "repaired playable must survive the English filter")
        self.assertIn("ext-cubs-brewers-123", after[0]["deeplink_play"])

    def test_reimport_resetting_deeplink_gets_refixed_and_reflagged(self):
        candidates = fix_espn_spanish_only.find_spanish_only_events(self.conn)
        fix_espn_spanish_only.fix_spanish_only_playables(self.conn, candidates)

        # Simulate the next day's Apple TV re-scrape resetting deeplink_play
        # back to the raw Spanish playID (locale/locale_fallback untouched,
        # since fruit_import_appletv.py's upsert never writes those columns).
        self.conn.execute(
            "UPDATE playables SET deeplink_play = ? WHERE event_id = 'evt-cubs'",
            ("sportscenter://x-callback-url/showWatchStream?playID=es-only-playid",),
        )
        self.conn.commit()

        candidates_day2 = fix_espn_spanish_only.find_spanish_only_events(self.conn)
        self.assertEqual(len(candidates_day2), 1, "must still be re-detected via locale='es_MX'")
        updated = fix_espn_spanish_only.fix_spanish_only_playables(self.conn, candidates_day2)
        self.assertEqual(updated, 1)

        after = filter_integration.get_filtered_playables(
            self.conn, "evt-cubs", enabled_services=[], language_preference="en",
        )
        self.assertEqual(len(after), 1)


if __name__ == "__main__":
    unittest.main()
