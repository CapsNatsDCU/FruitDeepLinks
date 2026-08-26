import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from fruit_import_appletv import upsert_playables  # noqa: E402


class ImportPreservesSplitChildrenTest(unittest.TestCase):
    """upsert_playables() (the daily Apple TV re-import) must not delete
    entitlement-split child playable rows created by fruit_enrich_espn.py --
    they're never present in Apple's own playables list, since they're
    derived, not scraped. fruit_enrich_espn.py manages their lifecycle
    itself (deletes + recreates them fresh every run); this import step
    must leave them alone in both the "still-offered" and "event has zero
    playables now" branches.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
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
                priority INTEGER DEFAULT 0,
                created_utc TEXT,
                http_deeplink_url TEXT,
                espn_graph_id TEXT,
                service_name TEXT,
                logical_service TEXT,
                locale TEXT,
                locale_fallback INTEGER DEFAULT 0,
                PRIMARY KEY (event_id, playable_id)
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO playables (
                event_id, playable_id, provider, logical_service, espn_graph_id, locale
            ) VALUES (
                'evt-1', 'apple-playable-1::espn-ent:unlimited-playback-id', 'sportscenter',
                'espn_unlimited', 'unlimited-playback-id', 'en_US'
            )
            """
        )
        self.conn.commit()

    def test_child_survives_when_parent_still_offered(self):
        fresh = [(
            "evt-1", "apple-playable-1", "sportscenter", "ESPN Unlimited", "espn_mlb_tv",
            "sportscenter://x-callback-url/showWatchStream?playID=mlbtv-playback-id",
            "sportscenter://x-callback-url/showWatchStream?playID=mlbtv-playback-id",
            None, "Chicago Cubs vs. Arizona Diamondbacks", "apple-playable-1", 26, None, "2026-01-01T00:00:00Z",
        )]
        upsert_playables(self.conn, "evt-1", fresh)

        rows = self.conn.execute(
            "SELECT playable_id FROM playables WHERE event_id = 'evt-1' ORDER BY playable_id"
        ).fetchall()
        ids = [r[0] for r in rows]
        self.assertIn("apple-playable-1", ids)
        self.assertIn("apple-playable-1::espn-ent:unlimited-playback-id", ids, "child row must survive re-import")

    def test_child_survives_when_event_has_zero_playables_this_run(self):
        # e.g. Apple temporarily returns no playables for this event at all.
        upsert_playables(self.conn, "evt-1", [])

        rows = self.conn.execute(
            "SELECT playable_id FROM playables WHERE event_id = 'evt-1'"
        ).fetchall()
        ids = [r[0] for r in rows]
        self.assertIn("apple-playable-1::espn-ent:unlimited-playback-id", ids, "child row must survive even a zero-playable re-import")


if __name__ == "__main__":
    unittest.main()
