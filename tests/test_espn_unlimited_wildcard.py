import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import filter_integration  # noqa: E402


class EspnUnlimitedWildcardTest(unittest.TestCase):
    """Regression test, now covering both directions of the same bug.

    Originally: a user who only checked "ESPN Unlimited" in Filters (not
    knowing espn_mlb_tv/espn_mlb_network exist as separate, granular
    services) got zero valid links for MLB events where Apple's catalog
    carves the English broadcast out into espn_mlb_tv while leaving only a
    Spanish playable under the generic espn_unlimited entry -- e.g. the real
    "MLB: Cincinnati Reds at Chicago Cubs" event: espn_unlimited playable
    was es_MX-only, espn_mlb_tv carried the actual English playables. Fixed
    by wildcarding 'espn_unlimited' -> also-covers-granular-tiers, applied
    live on every filter evaluation (mirroring the 'aiv' -> aiv_* wildcard).

    That live wildcard turned out to be its own bug (reported on the forum):
    it couldn't distinguish "user never saw this checkbox" from "user
    explicitly unchecked ESPN MLB.TV" -- both are just an absence from
    enabled_services -- so it silently re-included MLB.TV on every request
    even after an explicit uncheck. Fixed by removing the wildcard from live
    filtering entirely; migrate_backfill_espn_unlimited_granular_tiers.py
    now applies it ONCE to existing saved preferences instead, preserving
    old behavior for anyone who genuinely never considered these tiers
    without perpetually overriding new, explicit choices.

    expand_enabled_services_for_espn_unlimited() itself is unchanged --
    it's just no longer called from get_filtered_playables() live; only
    from that one-time migration.
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

    def test_live_filtering_no_longer_wildcards_espn_unlimited_to_mlb_tv(self):
        # A user whose saved enabled_services is just ["espn_unlimited"] --
        # e.g. never migrated, or explicitly re-saved after unchecking ESPN
        # MLB.TV -- must NOT have espn_mlb_tv playables silently reappear.
        # (Getting espn_mlb_tv's English playables for someone who never
        # considered the tier at all is now migrate_backfill_
        # espn_unlimited_granular_tiers.py's one-time job, not live filtering's.)
        result = filter_integration.get_filtered_playables(
            self.conn, "evt-cubs", enabled_services=["espn_unlimited"], language_preference="en",
        )
        self.assertEqual(
            result, [],
            "espn_mlb_tv playables must not be live-wildcarded in under bare "
            "espn_unlimited -- that silently overrides an explicit uncheck",
        )

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
