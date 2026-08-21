import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import filter_integration  # noqa: E402


class EspnLocaleLanguageFilterTest(unittest.TestCase):
    """Regression test for community report #787: ESPN Unlimited playables
    share a generic service_name ("ESPN Unlimited") for both English and
    Spanish entitlements, so the old service_name-only Spanish detection
    never fired and the Spanish playable won on raw DB priority instead.
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
                locale TEXT
            )
            """
        )

        # Mirrors the JSON pasted in the forum report: three ESPN Unlimited
        # playables for the same event, all sharing the generic service_name,
        # distinguished only by locale and title. The Spanish playable has
        # the lowest (best) raw DB priority.
        rows = [
            ("35f80c49-ab84-46d6-9076-7a0e17c3c898", "Braves vs Brewers", 26, "en_US"),
            ("01d04a46-e75b-4200-93ba-929fedf2c43a", "Braves vs Brewers En Español", 1, "es_MX"),
            ("55097618-cba9-4dc7-95ba-d8fa9e1631fd", "Braves vs Brewers", 26, "en_US"),
        ]
        for playable_id, title, priority, locale in rows:
            self.conn.execute(
                """
                INSERT INTO playables (
                    event_id, playable_id, provider, deeplink_play, deeplink_open,
                    playable_url, title, content_id, priority, service_name,
                    logical_service, locale
                ) VALUES (?, ?, 'sportscenter', ?, ?, ?, ?, ?, ?, 'ESPN Unlimited', 'espn_unlimited', ?)
                """,
                (
                    "evt-1", playable_id,
                    f"sportscenter://x-callback-url/showEvent?playableId={playable_id}",
                    f"sportscenter://x-callback-url/showEvent?playableId={playable_id}",
                    f"https://plus.espn.com/watch/{playable_id}",
                    title, playable_id, priority, locale,
                ),
            )
        self.conn.commit()

    def test_english_preference_excludes_spanish_locale_playable(self):
        filtered = filter_integration.get_filtered_playables(
            self.conn, "evt-1", enabled_services=[], language_preference="en",
        )
        self.assertTrue(filtered, "expected at least one playable to survive filtering")
        self.assertTrue(
            all(p["locale"] != "es_MX" for p in filtered),
            f"Spanish-locale playable leaked through: {[p['playable_id'] for p in filtered]}",
        )

    def test_best_playable_for_event_is_english(self):
        best = filter_integration.get_best_playable_for_event(
            self.conn, "evt-1", enabled_services=[], language_preference="en",
        )
        self.assertIsNotNone(best)
        self.assertEqual(best["locale"], "en_US")

    def test_spanish_preference_excludes_english_locale_playables(self):
        filtered = filter_integration.get_filtered_playables(
            self.conn, "evt-1", enabled_services=[], language_preference="es",
        )
        self.assertTrue(filtered)
        self.assertTrue(all(p["locale"] == "es_MX" for p in filtered))

    def test_both_preference_still_prefers_english_as_tiebreak(self):
        best = filter_integration.get_best_playable_for_event(
            self.conn, "evt-1", enabled_services=[], language_preference="both",
        )
        self.assertIsNotNone(best)
        self.assertEqual(best["locale"], "en_US")

    def test_applies_to_split_mlb_entitlement_tiers_too(self):
        # fruit_enrich_espn.py splits MLB.TV / MLB Network entitlements out of
        # the generic ESPN Unlimited/Plus buckets into their own logical
        # services. Locale-based language filtering must not be gated to
        # espn_unlimited/espn_plus/espn_linear -- it has to hold for every
        # tier migrate_add_locale.py backfills locale for.
        for logical_service in ("espn_mlb_tv", "espn_mlb_network"):
            with self.subTest(logical_service=logical_service):
                self.conn.execute(
                    """
                    INSERT INTO playables (
                        event_id, playable_id, provider, deeplink_play, deeplink_open,
                        playable_url, title, content_id, priority, service_name,
                        logical_service, locale
                    ) VALUES (?, ?, 'sportscenter', ?, ?, ?, ?, ?, ?, 'ESPN Unlimited', ?, ?)
                    """,
                    (
                        f"evt-{logical_service}", f"{logical_service}-es", "deep://es", "open://es",
                        "https://plus.espn.com/watch/es", "Braves vs Brewers En Español",
                        f"{logical_service}-es", 1, logical_service, "es_MX",
                    ),
                )
                self.conn.execute(
                    """
                    INSERT INTO playables (
                        event_id, playable_id, provider, deeplink_play, deeplink_open,
                        playable_url, title, content_id, priority, service_name,
                        logical_service, locale
                    ) VALUES (?, ?, 'sportscenter', ?, ?, ?, ?, ?, ?, 'ESPN Unlimited', ?, ?)
                    """,
                    (
                        f"evt-{logical_service}", f"{logical_service}-en", "deep://en", "open://en",
                        "https://plus.espn.com/watch/en", "Braves vs Brewers",
                        f"{logical_service}-en", 26, logical_service, "en_US",
                    ),
                )
                self.conn.commit()

                best = filter_integration.get_best_playable_for_event(
                    self.conn, f"evt-{logical_service}", enabled_services=[],
                    language_preference="en",
                )
                self.assertIsNotNone(best)
                self.assertEqual(best["locale"], "en_US")


if __name__ == "__main__":
    unittest.main()
