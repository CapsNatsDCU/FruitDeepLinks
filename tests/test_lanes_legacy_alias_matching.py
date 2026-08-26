import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from server.services.lanes import get_provider_playable_link  # noqa: E402


class LanesLegacyAliasMatchingTest(unittest.TestCase):
    """Regression test: get_provider_playable_link() (ADB tune-time deeplink
    resolution) builds its SQL candidate set from
    adb_provider_mapper.get_logical_services_for_adb_provider(), which only
    lists canonical service codes. A playable still tagged with a legacy
    alias (e.g. 'aiv_watch_for_free', pre-dating the aiv_free rename) is
    invisible to that raw `logical_service IN (...)` comparison -- even
    though the get_filtered_playables() call further down in the same
    function correctly normalizes and picks it as "preferred", the final
    SELECT that actually fetches the deeplink would miss it entirely if it's
    the only candidate, since expand_with_legacy_aliases() wasn't applied to
    `mapped` before this fix.
    """

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

    def set_preferences(self, enabled_services):
        self.conn.execute(
            "INSERT OR REPLACE INTO user_preferences (key, value) VALUES ('enabled_services', ?)",
            (json.dumps(enabled_services),),
        )
        self.conn.commit()

    def insert_playable(self, playable_id, logical_service):
        scheme = f"aiv://aiv/detail?gti=amzn1.dv.gti.content&broadcast={playable_id}"
        self.conn.execute(
            """
            INSERT INTO playables (
                event_id, playable_id, provider, deeplink_play, deeplink_open,
                playable_url, title, content_id, priority, created_utc,
                http_deeplink_url, espn_graph_id, service_name, logical_service, locale
            ) VALUES (
                'event-1', ?, 'aiv', ?, ?, '', '', '', 10, '', '', '', '', ?, ''
            )
            """,
            (playable_id, scheme, scheme, logical_service),
        )
        self.conn.commit()

    def test_resolves_deeplink_for_legacy_tagged_only_playable(self):
        self.insert_playable("amzn1.dv.gti.free", "aiv_watch_for_free")
        self.set_preferences(["aiv_free"])

        result = get_provider_playable_link(self.conn, "event-1", "aiv")

        self.assertIsNotNone(result["deeplink"], "must resolve a deeplink, not the empty fallback")
        self.assertIn("broadcast=amzn1.dv.gti.free", result["deeplink"])


if __name__ == "__main__":
    unittest.main()
