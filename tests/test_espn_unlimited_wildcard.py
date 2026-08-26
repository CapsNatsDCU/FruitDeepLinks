import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import filter_integration  # noqa: E402


class EspnUnlimitedWildcardTest(unittest.TestCase):
    """Regression test: a user who only checks "ESPN Unlimited" in Filters
    (not knowing espn_mlb_tv/espn_mlb_network exist as separate, granular
    services) got zero valid links for MLB events where Apple's catalog
    carves the English broadcast out into espn_mlb_tv while leaving only a
    Spanish playable under the generic espn_unlimited entry -- e.g. the real
    "MLB: Cincinnati Reds at Chicago Cubs" event found while investigating
    the ESPN Unlimited report: espn_unlimited playable was es_MX-only,
    espn_mlb_tv carried the actual English playables.

    expand_enabled_services_for_espn_unlimited() / the matching wildcard
    branch in get_filtered_playables() mirrors the existing 'aiv' -> aiv_*
    wildcard: 'espn_unlimited' alone also covers its granular MLB tiers,
    unless the user has explicitly picked one of those tiers themselves.
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
                http_deeplink_url TEXT,
                espn_graph_id TEXT,
                service_name TEXT,
                logical_service TEXT,
                locale TEXT,
                locale_fallback INTEGER DEFAULT 0
            )
            """
        )
        rows = [
            ("p-unlimited-es", "espn_unlimited", "es_MX", "Cincinnati Reds vs. Chicago Cubs (Espanol)", 1),
            ("p-mlbtv-en-1", "espn_mlb_tv", "en_US", "Cincinnati Reds vs. Chicago Cubs", 26),
            ("p-mlbtv-en-2", "espn_mlb_tv", "en_US", "Cincinnati Reds vs. Chicago Cubs", 26),
        ]
        for playable_id, logical_service, locale, title, priority in rows:
            self.conn.execute(
                """
                INSERT INTO playables (
                    event_id, playable_id, provider, deeplink_play, deeplink_open,
                    playable_url, title, content_id, priority, service_name,
                    logical_service, locale
                ) VALUES ('evt-cubs', ?, 'sportscenter', ?, ?, ?, ?, ?, ?, 'ESPN Unlimited', ?, ?)
                """,
                (
                    playable_id,
                    f"sportscenter://x-callback-url/showWatchStream?playID={playable_id}",
                    f"sportscenter://x-callback-url/showWatchStream?playID={playable_id}",
                    f"https://plus.espn.com/watch/{playable_id}",
                    title, playable_id, priority, logical_service, locale,
                ),
            )
        self.conn.commit()

    def test_espn_unlimited_only_still_surfaces_mlb_tv_english_playables(self):
        before_fix_note = filter_integration.get_filtered_playables(
            self.conn, "evt-cubs", enabled_services=["espn_unlimited"], language_preference="en",
        )
        self.assertTrue(
            before_fix_note,
            "with the wildcard, enabling only espn_unlimited must still surface the "
            "event's English espn_mlb_tv playables instead of returning nothing",
        )
        self.assertTrue(all(p["logical_service"] == "espn_mlb_tv" for p in before_fix_note))
        self.assertEqual(len(before_fix_note), 2)

    def test_explicit_granular_tier_selection_overrides_wildcard(self):
        # User explicitly picked espn_mlb_tv (without espn_unlimited) --
        # espn_mlb_network must NOT be silently included.
        result = filter_integration.get_filtered_playables(
            self.conn, "evt-cubs", enabled_services=["espn_mlb_tv"], language_preference="en",
        )
        self.assertTrue(all(p["logical_service"] == "espn_mlb_tv" for p in result))

    def test_wildcard_does_not_apply_once_a_granular_tier_is_explicit(self):
        # enabled_services has BOTH espn_unlimited and espn_mlb_tv explicitly --
        # per the aiv-style override rule, the explicit list is authoritative,
        # so this must behave identically to the "only espn_mlb_tv" case, not
        # additionally reach for espn_mlb_network.
        result = filter_integration.expand_enabled_services_for_espn_unlimited(
            ["espn_unlimited", "espn_mlb_tv"]
        )
        self.assertEqual(result, ["espn_unlimited", "espn_mlb_tv"])

    def test_no_espn_unlimited_means_no_wildcard(self):
        result = filter_integration.expand_enabled_services_for_espn_unlimited(["peacock_web"])
        self.assertEqual(result, ["peacock_web"])

    def test_empty_or_espn_unlimited_alone_wildcard_expands(self):
        result = filter_integration.expand_enabled_services_for_espn_unlimited(["espn_unlimited"])
        self.assertEqual(set(result), {"espn_unlimited", "espn_mlb_tv", "espn_mlb_network"})


if __name__ == "__main__":
    unittest.main()
