import json
import logging
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import filter_integration  # noqa: E402
from fruit_build_adb_lanes import (  # noqa: E402
    build_adb_lanes,
    load_user_preferences as load_adb_preferences,
)
from server.services.lanes import (  # noqa: E402
    _load_user_preferences_cached,
    get_provider_playable_link,
)


class AmazonAdbPlayableSelectionTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
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
                priority INTEGER,
                created_utc TEXT,
                http_deeplink_url TEXT,
                espn_graph_id TEXT,
                service_name TEXT,
                logical_service TEXT,
                locale TEXT
            )
            """
        )
        self.conn.execute(
            "CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT)"
        )

    def set_preferences(self, enabled_services, amazon_master_enabled=True):
        values = {
            "enabled_services": enabled_services,
            "amazon_master_enabled": amazon_master_enabled,
        }
        self.conn.executemany(
            "INSERT OR REPLACE INTO user_preferences (key, value) VALUES (?, ?)",
            [(key, json.dumps(value)) for key, value in values.items()],
        )
        self.conn.commit()

    def insert_playable(self, playable_id, logical_service, priority=27):
        scheme = (
            "aiv://aiv/detail?gti=amzn1.dv.gti.content"
            f"&broadcast={playable_id}"
        )
        http = f"https://app.primevideo.com/detail?gti={playable_id}"
        self.conn.execute(
            """
            INSERT INTO playables (
                event_id, playable_id, provider, deeplink_play, deeplink_open,
                playable_url, title, content_id, priority, created_utc,
                http_deeplink_url, espn_graph_id, service_name, logical_service,
                locale
            ) VALUES (
                'event-1', ?, 'aiv', ?, ?, '', '', '', ?, '', ?, '', '', ?, ''
            )
            """,
            (playable_id, scheme, scheme, priority, http, logical_service),
        )
        self.conn.commit()

    def test_matches_filtered_best_across_amazon_subservices(self):
        self.insert_playable("amzn1.dv.gti.dazn", "aiv_dazn")
        self.insert_playable("amzn1.dv.gti.free", "aiv_free")
        self.set_preferences(["aiv_dazn", "aiv_free"])

        expected = filter_integration.get_filtered_playables(
            self.conn,
            "event-1",
            ["aiv_dazn", "aiv_free"],
            amazon_master_enabled=True,
        )[0]
        actual = get_provider_playable_link(self.conn, "event-1", "aiv")

        self.assertEqual(actual["playable_id"], expected["playable_id"])
        self.assertIn(f"broadcast={expected['playable_id']}", actual["deeplink"])
        self.assertEqual(
            actual["http_deeplink_url"],
            f"https://app.primevideo.com/detail?gti={expected['playable_id']}",
        )

    def test_matches_filtered_best_among_same_service_siblings(self):
        self.insert_playable("amzn1.dv.gti.z-sibling", "aiv_prime")
        self.insert_playable("amzn1.dv.gti.a-sibling", "aiv_prime")
        self.set_preferences(["aiv_prime"])

        expected = filter_integration.get_filtered_playables(
            self.conn,
            "event-1",
            ["aiv_prime"],
            amazon_master_enabled=True,
        )[0]
        actual = get_provider_playable_link(self.conn, "event-1", "aiv")

        self.assertEqual(actual["playable_id"], expected["playable_id"])

    def test_does_not_fall_back_to_disabled_amazon_service(self):
        self.insert_playable("amzn1.dv.gti.dazn", "aiv_dazn", priority=1)
        self.insert_playable("amzn1.dv.gti.prime", "aiv_prime", priority=99)
        self.set_preferences(["aiv_prime"])

        actual = get_provider_playable_link(self.conn, "event-1", "aiv")

        self.assertEqual(actual["playable_id"], "amzn1.dv.gti.prime")

    def test_returns_no_link_when_amazon_master_is_disabled(self):
        self.insert_playable("amzn1.dv.gti.prime", "aiv_prime")
        self.set_preferences(["aiv_prime"], amazon_master_enabled=False)

        actual = get_provider_playable_link(self.conn, "event-1", "aiv")

        self.assertIsNone(actual["deeplink"])
        self.assertIsNone(actual["playable_id"])

    def test_pinned_playable_respects_disabled_amazon_master(self):
        self.insert_playable("amzn1.dv.gti.prime", "aiv_prime")
        self.set_preferences(["aiv_prime"], amazon_master_enabled=False)

        actual = get_provider_playable_link(
            self.conn,
            "event-1",
            "aiv",
            playable_id="amzn1.dv.gti.prime",
        )

        self.assertIsNone(actual["deeplink"])
        self.assertIsNone(actual["playable_id"])

    def test_adb_builder_normalizes_legacy_enabled_service_alias(self):
        self.set_preferences(["aiv_watch_for_free"])

        prefs = load_adb_preferences(self.conn, logging.getLogger(__name__))

        self.assertEqual(prefs["enabled_services"], ["aiv_free"])

    def test_preferences_cache_is_isolated_by_database_path(self):
        def open_preferences_db(path, enabled_services):
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "INSERT INTO user_preferences (key, value) VALUES (?, ?)",
                ("enabled_services", json.dumps(enabled_services)),
            )
            conn.commit()
            return conn

        with tempfile.TemporaryDirectory() as temp_dir:
            conn1 = open_preferences_db(Path(temp_dir) / "one.db", ["aiv_prime"])
            conn2 = open_preferences_db(Path(temp_dir) / "two.db", ["aiv_free"])
            try:
                prefs1 = _load_user_preferences_cached(conn1)
                prefs2 = _load_user_preferences_cached(conn2)

                self.assertEqual(prefs1["enabled_services"], ["aiv_prime"])
                self.assertEqual(prefs2["enabled_services"], ["aiv_free"])
            finally:
                conn1.close()
                conn2.close()

    def test_builder_clears_amazon_lanes_when_master_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "lanes.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE adb_lanes (provider_code TEXT, lane_number INTEGER, channel_id TEXT, event_id TEXT, start_utc TEXT, stop_utc TEXT, playable_id TEXT, playable_label TEXT);
                CREATE TABLE provider_lanes (provider_code TEXT, adb_enabled INTEGER, adb_lane_count INTEGER);
                CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO provider_lanes VALUES ('aiv', 1, 1);
                INSERT INTO adb_lanes VALUES ('aiv', 1, 'aiv01', 'old-event', '2026-01-01', '2026-01-02', 'old-playable', 'Prime');
                """
            )
            conn.execute(
                "INSERT INTO user_preferences (key, value) VALUES (?, ?)",
                ("amazon_master_enabled", json.dumps(False)),
            )
            conn.commit()
            conn.close()

            build_adb_lanes(str(db_path))

            with sqlite3.connect(db_path) as check_conn:
                remaining = check_conn.execute("SELECT COUNT(*) FROM adb_lanes").fetchone()[0]

            self.assertEqual(remaining, 0)

if __name__ == "__main__":
    unittest.main()
