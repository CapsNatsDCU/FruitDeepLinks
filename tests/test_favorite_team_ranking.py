import json
import sqlite3
import sys
import unittest
from pathlib import Path


BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import filter_integration
from db.preferences import load_all_settings, save_settings
from team_preferences import find_favorite_teams, score_team_affinity


CAPITALS = {
    "team": "Washington Capitals",
    "aliases": ["Capitals", "WSH"],
    "preferred_terms": ["WASHINGTON CAPITALS", "MONUMENTAL"],
    "avoid_terms": [],
    "enabled": True,
}


class TeamPreferenceUnitTests(unittest.TestCase):
    def test_event_matching_uses_complete_tokens_not_substrings(self):
        self.assertEqual(
            ["Washington Capitals"],
            [
                team["team"]
                for team in find_favorite_teams(
                    {"title": "Capitals vs Rangers"}, [CAPITALS]
                )
            ],
        )
        self.assertEqual(
            [],
            find_favorite_teams(
                {"title": "A discussion of capitalistic markets"}, [CAPITALS]
            ),
        )

    def test_explicit_avoid_term_is_a_penalty_not_a_filter(self):
        favorite = dict(CAPITALS, avoid_terms=["BAD CALL"])
        scored = score_team_affinity(
            {"title": "Capitals vs Rangers"},
            {"title": "Capitals Broadcast - BAD CALL"},
            [favorite],
        )
        self.assertEqual(50, scored["score"])
        self.assertEqual([100, -50], [item["score"] for item in scored["reasons"]])


class FavoriteTeamSelectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            CREATE TABLE events (
                id TEXT PRIMARY KEY,
                title TEXT,
                title_brief TEXT,
                synopsis TEXT,
                channel_name TEXT,
                classification_json TEXT,
                genres_json TEXT,
                raw_attributes_json TEXT
            );
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
                service_name TEXT,
                espn_graph_id TEXT,
                logical_service TEXT,
                locale TEXT,
                feed_name TEXT,
                feed_type TEXT,
                http_deeplink_url TEXT,
                stream_metadata_json TEXT,
                category_name TEXT,
                subcategory_name TEXT,
                network TEXT
            );
            CREATE TABLE user_preferences (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_utc TEXT
            );
            """
        )

    def tearDown(self):
        self.conn.close()

    def event(self, title):
        self.conn.execute(
            "INSERT INTO events (id, title, classification_json, genres_json) "
            "VALUES ('event', ?, '[]', '[\"Sports\"]')",
            (title,),
        )

    def playable(
        self,
        playable_id,
        title,
        provider="xtream",
        service="xtream",
        service_name=None,
        feed_name=None,
        feed_type=None,
        metadata=None,
        priority=10,
    ):
        self.conn.execute(
            """
            INSERT INTO playables (
                event_id, playable_id, provider, deeplink_play, title, content_id,
                priority, service_name, logical_service, locale, feed_name,
                feed_type, stream_metadata_json
            ) VALUES ('event', ?, ?, ?, ?, ?, ?, ?, ?, 'en_US', ?, ?, ?)
            """,
            (
                playable_id,
                provider,
                f"{provider}://{playable_id}",
                title,
                playable_id,
                priority,
                service_name or service,
                service,
                feed_name,
                feed_type,
                json.dumps(metadata) if metadata else None,
            ),
        )

    def preferences(self, enabled=True, teams=None):
        self.assertTrue(save_settings(self.conn, {
            "prefer_favorite_team_broadcaster": enabled,
            "favorite_teams": teams if teams is not None else [CAPITALS],
        }))

    def best(self, priority_map=None):
        return filter_integration.get_best_playable_for_event(
            self.conn,
            "event",
            [],
            priority_map=priority_map or {"xtream": 50, "espn_plus": 100},
        )

    def test_a_preferred_team_stream_wins(self):
        self.event("Capitals vs Rangers")
        self.playable("a-generic", "US: NHL PPV Capitals vs Rangers")
        self.playable("b-rangers", "US: NEW YORK RANGERS")
        self.playable("z-capitals", "US: WASHINGTON CAPITALS")
        self.preferences()
        self.assertEqual("z-capitals", self.best()["playable_id"])

    def test_b_preferred_broadcaster_wins_across_service_priority(self):
        self.event("Capitals vs Penguins")
        self.playable("a-espn", "ESPN+", provider="sportscenter", service="espn_plus")
        self.playable("b-generic", "generic NHL PPV")
        self.playable("z-monumental", "Monumental Sports Network")
        self.preferences()
        winner = self.best()
        self.assertEqual("z-monumental", winner["playable_id"])
        self.assertEqual(70, winner["team_preference"]["score"])

    def test_c_missing_preferred_stream_uses_existing_best_fallback(self):
        self.event("Capitals vs Penguins")
        self.playable("a-generic", "generic NHL PPV")
        self.playable("z-espn", "ESPN+", provider="sportscenter", service="espn_plus")
        self.preferences()
        self.assertEqual("z-espn", self.best()["playable_id"])

    def test_d_opponent_feed_loses(self):
        self.event("Capitals vs Rangers")
        self.playable("a-rangers", "Rangers Broadcast")
        self.playable("z-capitals", "Capitals Broadcast")
        self.preferences()
        self.assertEqual("z-capitals", self.best()["playable_id"])

    def test_e_named_favorite_feed_wins_when_team_is_away(self):
        self.event("Rangers vs Capitals")
        self.playable("a-rangers", "Rangers Broadcast")
        self.playable("z-capitals", "Capitals Broadcast")
        self.preferences()
        self.assertEqual("z-capitals", self.best()["playable_id"])

    def test_f_no_preference_configuration_preserves_baseline(self):
        self.event("Capitals vs Rangers")
        self.playable("a-rangers", "Rangers Broadcast")
        self.playable("z-capitals", "Capitals Broadcast")
        baseline = self.best()["playable_id"]
        self.preferences(enabled=True, teams=[])
        self.assertEqual(baseline, self.best()["playable_id"])

    def test_g_toggle_off_preserves_baseline(self):
        self.event("Capitals vs Rangers")
        self.playable("a-rangers", "Rangers Broadcast")
        self.playable("z-capitals", "Capitals Broadcast")
        baseline = self.best()["playable_id"]
        self.preferences(enabled=False)
        self.assertEqual(baseline, self.best()["playable_id"])

    def test_h_two_favorite_teams_is_deterministic(self):
        self.event("Capitals vs Rangers")
        self.playable("a-rangers", "Rangers Broadcast")
        self.playable("z-capitals", "Capitals Broadcast")
        rangers = {
            "team": "New York Rangers",
            "aliases": ["Rangers"],
            "preferred_terms": [],
            "avoid_terms": [],
            "enabled": True,
        }
        self.preferences(teams=[CAPITALS, rangers])
        winners = [self.best()["playable_id"] for _ in range(5)]
        self.assertEqual(["a-rangers"] * 5, winners)

    def test_i_xtream_stream_metadata_is_used(self):
        self.event("Capitals vs Rangers")
        self.playable("a-generic", "generic stream")
        self.playable(
            "z-capitals",
            "opaque stream",
            metadata={
                "original_name": "US: WASHINGTON CAPITALS",
                "category_name": "US| NHL TEAM PPV",
            },
        )
        self.preferences()
        self.assertEqual("z-capitals", self.best()["playable_id"])

    def test_j_espn_home_away_metadata_uses_unambiguous_event_role(self):
        self.event("Capitals at Rangers")
        self.playable(
            "a-home",
            "ESPN feed",
            provider="sportscenter",
            service="espn_plus",
            feed_name="Rangers Broadcast",
            feed_type="HOME",
        )
        self.playable(
            "z-away",
            "ESPN feed",
            provider="sportscenter",
            service="espn_plus",
            feed_name="Capitals Broadcast",
            feed_type="AWAY",
        )
        self.preferences()
        winner = self.best()
        self.assertEqual("z-away", winner["playable_id"])
        self.assertEqual(140, winner["team_preference"]["score"])

    def test_persisted_settings_flow_into_resolved_deeplink_selection(self):
        """End-to-end-ish: SQLite settings -> shared ranker -> resolved link."""
        self.event("Capitals vs Penguins")
        self.playable("a-espn", "ESPN+", provider="sportscenter", service="espn_plus")
        self.playable("z-monumental", "Monumental Sports Network")
        self.preferences()
        stored = load_all_settings(self.conn)
        self.assertTrue(stored["prefer_favorite_team_broadcaster"])
        self.assertEqual("Washington Capitals", stored["favorite_teams"][0]["team"])
        deeplink = filter_integration.get_best_deeplink_for_event(
            self.conn,
            "event",
            [],
            priority_map={"xtream": 50, "espn_plus": 100},
        )
        self.assertEqual("xtream://z-monumental", deeplink)


if __name__ == "__main__":
    unittest.main()
