import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import fruit_enrich_espn as enrich  # noqa: E402


def _make_fruit_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE events (
            id TEXT PRIMARY KEY,
            title TEXT,
            start_utc TEXT,
            raw_attributes_json TEXT
        )
        """
    )
    conn.execute(
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
    conn.commit()
    return conn


def _make_espn_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE events (
            id TEXT PRIMARY KEY,
            start_utc TEXT NOT NULL,
            stop_utc TEXT NOT NULL,
            title TEXT,
            network TEXT,
            packages TEXT,
            airing_id TEXT,
            simulcast_airing_id TEXT,
            program_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            url TEXT,
            is_primary INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.commit()
    return conn


class EspnEntitlementSplitTest(unittest.TestCase):
    """Regression test for the "Cubs vs. D'backs" report: ESPN Watch Graph
    exposes two feed candidates under one Apple playable's program_id -- one
    plain 'ESPN Unlimited', one gated behind the MLB_TV package -- and the
    old code always discarded the non-chosen one. A user with only
    espn_unlimited enabled (not espn_mlb_tv) got nothing for that game even
    though a genuine, working plain-Unlimited stream existed on ESPN's side.

    fruit_enrich_espn.py now creates a sibling playable row for the
    discarded entitlement instead of dropping it.
    """

    def setUp(self):
        self.fruit_dir = tempfile.TemporaryDirectory()
        self.espn_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.fruit_dir.cleanup)
        self.addCleanup(self.espn_dir.cleanup)
        self.fruit_db = str(Path(self.fruit_dir.name) / "fruit.db")
        self.espn_db = str(Path(self.espn_dir.name) / "espn.db")

        self.fconn = _make_fruit_db(self.fruit_db)
        self.econn = _make_espn_db(self.espn_db)

        program_id = "819d7c12-d133-4130-85e1-7b10177d2ae8"
        self.program_id = program_id
        raw_json = json.dumps({"playables": {"apple-playable-1": {"externalId": program_id}}})
        self.fconn.execute(
            "INSERT INTO events (id, title, start_utc, raw_attributes_json) VALUES (?, ?, ?, ?)",
            ("evt-1", "Chicago Cubs at Arizona Diamondbacks", "2026-08-26T19:00:00Z", raw_json),
        )
        self.fconn.execute(
            """
            INSERT INTO playables (
                event_id, playable_id, provider, deeplink_play, deeplink_open,
                playable_url, title, content_id, priority, service_name,
                logical_service, locale
            ) VALUES (
                'evt-1', 'apple-playable-1', 'sportscenter',
                'sportscenter://x-callback-url/showWatchStream?playID=apple-original',
                'sportscenter://x-callback-url/showWatchStream?playID=apple-original',
                NULL, 'Chicago Cubs vs. Arizona Diamondbacks', 'apple-playable-1', 26,
                'ESPN Unlimited', 'espn_unlimited', 'en_US'
            )
            """
        )
        self.fconn.commit()

    def _insert_espn_feed(self, espn_event_id, url, packages, network):
        self.econn.execute(
            "INSERT INTO events (id, start_utc, stop_utc, title, network, packages, program_id) "
            "VALUES (?, '2026-08-26T19:00:00Z', '2026-08-26T22:00:00Z', 'Cubs at D-backs', ?, ?, ?)",
            (espn_event_id, network, packages, self.program_id),
        )
        self.econn.execute(
            "INSERT INTO feeds (event_id, url) VALUES (?, ?)",
            (espn_event_id, url),
        )
        self.econn.commit()

    def _playables(self):
        cur = self.fconn.cursor()
        cur.execute(
            "SELECT playable_id, logical_service, espn_graph_id, deeplink_play, locale, locale_fallback "
            "FROM playables WHERE event_id = 'evt-1' ORDER BY playable_id"
        )
        return cur.fetchall()

    def test_two_entitlements_creates_sibling_playable(self):
        self._insert_espn_feed(
            "espn-watch:mlbtv-playback-id",
            "https://www.espn.com/watch/player/_/id/mlbtv-playback-id",
            '["MLB_TV"]', "MLB.TV",
        )
        self._insert_espn_feed(
            "espn-watch:unlimited-playback-id",
            "https://www.espn.com/watch/player/_/id/unlimited-playback-id",
            None, "ESPN Unlimited",
        )

        enrich.enrich_playables(self.fruit_db, self.espn_db, dry_run=False, skip_enrich=False)

        # Re-open -- enrich_playables uses its own connections
        conn = sqlite3.connect(self.fruit_db)
        rows = conn.execute(
            "SELECT playable_id, logical_service, espn_graph_id, deeplink_play, locale, locale_fallback "
            "FROM playables WHERE event_id = 'evt-1' ORDER BY playable_id"
        ).fetchall()

        self.assertEqual(len(rows), 2, f"expected primary + 1 split child, got: {rows}")

        primary = next(r for r in rows if r[0] == "apple-playable-1")
        self.assertEqual(primary[1], "espn_mlb_tv", "primary keeps today's existing pick (packages-preferring)")
        self.assertEqual(primary[2], "mlbtv-playback-id")

        child = next(r for r in rows if r[0] != "apple-playable-1")
        self.assertIn("apple-playable-1::espn-ent:", child[0])
        self.assertEqual(child[1], "espn_unlimited", "discarded entitlement must survive as its own row")
        self.assertEqual(child[2], "unlimited-playback-id")
        self.assertIn("unlimited-playback-id", child[3])
        self.assertEqual(child[4], "en_US", "child inherits parent's locale")
        self.assertEqual(child[5], 0)

    def test_single_entitlement_creates_no_sibling(self):
        # Only one Watch Graph candidate at all -- today's common case, must
        # be completely unaffected by the new splitting logic.
        self._insert_espn_feed(
            "espn-watch:only-candidate",
            "https://www.espn.com/watch/player/_/id/only-candidate",
            None, "ESPN Unlimited",
        )

        enrich.enrich_playables(self.fruit_db, self.espn_db, dry_run=False, skip_enrich=False)

        rows = self._playables()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "apple-playable-1")
        self.assertEqual(rows[0][2], "only-candidate")

    def test_idempotent_rerun_does_not_duplicate(self):
        self._insert_espn_feed(
            "espn-watch:mlbtv-playback-id",
            "https://www.espn.com/watch/player/_/id/mlbtv-playback-id",
            '["MLB_TV"]', "MLB.TV",
        )
        self._insert_espn_feed(
            "espn-watch:unlimited-playback-id",
            "https://www.espn.com/watch/player/_/id/unlimited-playback-id",
            None, "ESPN Unlimited",
        )

        enrich.enrich_playables(self.fruit_db, self.espn_db, dry_run=False, skip_enrich=False)
        enrich.enrich_playables(self.fruit_db, self.espn_db, dry_run=False, skip_enrich=False)

        rows = self._playables()
        self.assertEqual(len(rows), 2, f"re-running must not duplicate the split child: {rows}")

    def test_child_self_cleans_when_second_entitlement_disappears(self):
        self._insert_espn_feed(
            "espn-watch:mlbtv-playback-id",
            "https://www.espn.com/watch/player/_/id/mlbtv-playback-id",
            '["MLB_TV"]', "MLB.TV",
        )
        self._insert_espn_feed(
            "espn-watch:unlimited-playback-id",
            "https://www.espn.com/watch/player/_/id/unlimited-playback-id",
            None, "ESPN Unlimited",
        )
        enrich.enrich_playables(self.fruit_db, self.espn_db, dry_run=False, skip_enrich=False)
        self.assertEqual(len(self._playables()), 2)

        # Simulate the next day's Watch Graph scrape no longer offering the
        # plain-Unlimited feed for this program_id (e.g. it rolled out of
        # ESPN's own catalog).
        self.econn.execute("DELETE FROM feeds WHERE event_id = 'espn-watch:unlimited-playback-id'")
        self.econn.execute("DELETE FROM events WHERE id = 'espn-watch:unlimited-playback-id'")
        self.econn.commit()

        enrich.enrich_playables(self.fruit_db, self.espn_db, dry_run=False, skip_enrich=False)

        rows = self._playables()
        self.assertEqual(len(rows), 1, "stale split child must be removed once no longer justified")
        self.assertEqual(rows[0][0], "apple-playable-1")


if __name__ == "__main__":
    unittest.main()
