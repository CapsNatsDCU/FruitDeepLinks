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

    def test_named_deportes_only_event_is_never_fixed_or_shown_under_english(self):
        # Regression: spot-checking the locale_fallback fix found that a real,
        # distinctly-branded "ESPN Deportes" channel (not an ambiguous generic
        # label) got swept up by find_spanish_only_events() and flagged
        # locale_fallback=1 -- which then made _classify_espn_locale() treat
        # a genuinely Spanish-language broadcast as non-Spanish, so it started
        # showing under an English-only filter (real example: a Little League
        # game with only an ESPN Deportes broadcast). Unlike the generic
        # "ESPN Unlimited" label case above, there's no hidden English
        # broadcast behind a Deportes-branded feed for the "fix" to unlock.
        raw_json = json.dumps({"playables": {"p2": {"externalId": "ext-deportes-456"}}})
        self.conn.execute(
            "INSERT INTO events (id, raw_attributes_json) VALUES ('evt-deportes', ?)",
            (raw_json,),
        )
        self.conn.execute(
            """
            INSERT INTO playables (
                event_id, playable_id, provider, deeplink_play, deeplink_open,
                playable_url, title, content_id, priority, service_name,
                logical_service, locale
            ) VALUES (
                'evt-deportes', 'p2', 'sportscenter',
                'sportscenter://x-callback-url/showWatchStream?playID=deportes-only-playid',
                'sportscenter://x-callback-url/showWatchStream?playID=deportes-only-playid',
                'https://plus.espn.com/watch/deportes-only-playid',
                'Little League Baseball', 'p2', 26, 'ESPN Deportes',
                'espn_unlimited', 'es_MX'
            )
            """
        )
        self.conn.commit()

        candidates = fix_espn_spanish_only.find_spanish_only_events(self.conn)
        self.assertEqual(
            [c[0] for c in candidates], ["evt-cubs"],
            "named-Deportes event must not be treated as a fixable ambiguous label",
        )

        # Even if locale_fallback were set on it anyway (belt-and-suspenders:
        # don't rely solely on the SQL exclusion above), the classifier must
        # still treat it as Spanish.
        self.conn.execute(
            "UPDATE playables SET locale_fallback = 1 WHERE event_id = 'evt-deportes'"
        )
        self.conn.commit()

        result = filter_integration.get_filtered_playables(
            self.conn, "evt-deportes", enabled_services=[], language_preference="en",
        )
        self.assertEqual(result, [], "genuine Deportes-only broadcast must stay excluded under English")

    def test_matched_spanish_only_playable_is_not_a_fix_candidate(self):
        # fruit_enrich_espn.py now sets locale authoritatively from ESPN
        # Watch Graph's own language field for any matched playable (has
        # espn_graph_id set) -- if it's still es_MX after that, that's
        # confirmed reality, not something to rewrite or flag. This script's
        # job is now only the ~20% Watch Graph never matches at all.
        raw_json = json.dumps({"playables": {"p3": {"externalId": "ext-matched-789"}}})
        self.conn.execute(
            "INSERT INTO events (id, raw_attributes_json) VALUES ('evt-matched', ?)",
            (raw_json,),
        )
        self.conn.execute(
            """
            INSERT INTO playables (
                event_id, playable_id, provider, deeplink_play, deeplink_open,
                playable_url, title, content_id, priority, service_name,
                logical_service, locale, espn_graph_id
            ) VALUES (
                'evt-matched', 'p3', 'sportscenter',
                'sportscenter://x-callback-url/showWatchStream?playID=matched-spanish-playid',
                'sportscenter://x-callback-url/showWatchStream?playID=matched-spanish-playid',
                'https://plus.espn.com/watch/matched-spanish-playid',
                'Cubs vs Brewers', 'p3', 26, 'ESPN Unlimited',
                'espn_unlimited', 'es_MX', 'matched-spanish-playid'
            )
            """
        )
        self.conn.commit()

        candidates = fix_espn_spanish_only.find_spanish_only_events(self.conn)
        self.assertEqual(
            [c[0] for c in candidates], ["evt-cubs"],
            "matched (espn_graph_id set) event must not be treated as a fix candidate",
        )


if __name__ == "__main__":
    unittest.main()
