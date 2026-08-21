import logging
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from deeplink_converter import convert_amazon_prime  # noqa: E402
from migrate_add_adb_lanes import populate_http_deeplinks  # noqa: E402


CONTENT_GTI = "amzn1.dv.gti.dbcb7b1e-e53e-4bcb-b5fb-b19f270fa627"
BROADCAST_GTI = "amzn1.dv.gti.822acad6-3223-4896-976a-82e178fbc77d"


class AmazonHttpDeeplinkTest(unittest.TestCase):
    def test_prefers_broadcast_gti_for_feed_specific_web_link(self):
        punchout = (
            f"aiv://aiv/detail?gti={CONTENT_GTI}&action=watch&type=live"
            f"&broadcast={BROADCAST_GTI}"
        )

        self.assertEqual(
            convert_amazon_prime(punchout),
            f"https://app.primevideo.com/detail?gti={BROADCAST_GTI}",
        )

    def test_falls_back_to_content_gti_without_broadcast(self):
        punchout = f"aiv://aiv/detail?gti={CONTENT_GTI}&action=watch"

        self.assertEqual(
            convert_amazon_prime(punchout),
            f"https://app.primevideo.com/detail?gti={CONTENT_GTI}",
        )

    def test_prefill_upgrades_existing_content_level_amazon_link(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """
            CREATE TABLE playables (
                event_id TEXT NOT NULL,
                playable_id TEXT NOT NULL,
                provider TEXT,
                deeplink_play TEXT,
                http_deeplink_url TEXT,
                PRIMARY KEY (event_id, playable_id)
            )
            """
        )
        punchout = (
            f"aiv://aiv/detail?gti={CONTENT_GTI}&action=watch&type=live"
            f"&broadcast={BROADCAST_GTI}"
        )
        conn.execute(
            "INSERT INTO playables VALUES (?, ?, ?, ?, ?)",
            (
                "event-1",
                "playable-1",
                "aiv",
                punchout,
                f"https://app.primevideo.com/detail?gti={CONTENT_GTI}",
            ),
        )
        conn.commit()

        populate_http_deeplinks(conn, logging.getLogger(__name__))

        actual = conn.execute(
            "SELECT http_deeplink_url FROM playables WHERE event_id = 'event-1'"
        ).fetchone()[0]
        self.assertEqual(
            actual,
            f"https://app.primevideo.com/detail?gti={BROADCAST_GTI}",
        )


if __name__ == "__main__":
    unittest.main()
