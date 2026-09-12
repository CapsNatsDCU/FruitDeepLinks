import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from sports_metadata import ensure_schema, resolve_source_event
from local_ai_event_parser import LocalAIConfig


START = "2026-10-11T17:00:00Z"
AI_CONFIG = LocalAIConfig(True, "http://127.0.0.1:11434/v1", "test-model", 1, .8, 20, "deterministic_first")


class SportsInterpretationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def resolve(self, source_id, title, category="", **data):
        return resolve_source_event(self.conn, source="xtream", source_event_id=source_id, data={
            "title": title, "start_utc": START,
            "raw_attributes_json": json.dumps({"provider": "xtream", "category_name": category}),
            **data,
        }, ai_mode="disabled")

    def event_row(self, result):
        return self.conn.execute("""SELECT ce.*,s.name sport,l.name league FROM canonical_events ce
                                LEFT JOIN sports s ON s.id=ce.sport_id LEFT JOIN leagues l ON l.id=ce.league_id
                                WHERE ce.id=?""", (result["canonical_event_id"],)).fetchone()

    def members(self, result):
        return {(row["display_name"], row["role"]) for row in self.conn.execute(
            "SELECT display_name,role FROM canonical_event_participants WHERE event_id=?", (result["canonical_event_id"],))}

    def test_permanent_team_feeds_keep_association_but_are_not_events(self):
        nationals = self.resolve("nationals", "US: MLB WASHINGTON NATIONALS RAW", "US| MLB TEAM PPV")
        caps = self.resolve("caps", "US: WASHINGTON CAPITALS", "US| NHL TEAM PPV")
        self.assertFalse(nationals["scheduling_eligible"])
        self.assertEqual("single-team permanent feed", nationals["validation_reason"])
        self.assertEqual({("Washington Nationals", "participant")}, self.members(nationals))
        self.assertEqual(("baseball", "MLB", "no_event"), tuple(self.event_row(nationals)[key] for key in ("sport", "league", "event_type")))
        self.assertFalse(caps["scheduling_eligible"])
        self.assertEqual({("Washington Capitals", "participant")}, self.members(caps))
        self.assertEqual(("ice_hockey", "NHL", "no_event"), tuple(self.event_row(caps)[key] for key in ("sport", "league", "event_type")))

    def test_ambiguous_matchup_requires_provider_league_context(self):
        vague = self.resolve("vague", "WASHINGTON @ NEW YORK", "US| PPV EVENT")
        self.assertFalse(vague["scheduling_eligible"])
        self.assertEqual("ambiguous participants without league context", vague["validation_reason"])

        nhl = self.resolve("nhl", "WSH @ PHI", "US| NHL PPV")
        mlb = self.resolve("mlb", "WSH @ PHI", "US| MLB PPV")
        self.assertTrue(nhl["scheduling_eligible"])
        self.assertEqual({("Washington Capitals", "away"), ("Philadelphia Flyers", "home")}, self.members(nhl))
        self.assertEqual(("ice_hockey", "NHL", "Washington Capitals at Philadelphia Flyers"), tuple(self.event_row(nhl)[key] for key in ("sport", "league", "title")))
        self.assertTrue(mlb["scheduling_eligible"])
        self.assertEqual({("Washington Nationals", "away"), ("Philadelphia Phillies", "home")}, self.members(mlb))
        self.assertEqual(("baseball", "MLB"), tuple(self.event_row(mlb)[key] for key in ("sport", "league")))

    def test_normalized_display_names_and_non_game_programming(self):
        commanders = self.resolve("nfl", "Commanders at Eagles", "US| NFL PPV")
        mls = self.resolve("mls", "D.C. vs Atlanta", "US| MLS PPV")
        self.assertTrue(commanders["scheduling_eligible"])
        self.assertEqual(("american_football", "NFL", "Washington Commanders at Philadelphia Eagles"), tuple(self.event_row(commanders)[key] for key in ("sport", "league", "title")))
        self.assertEqual(("soccer", "MLS", "D.C. United at Atlanta United FC"), tuple(self.event_row(mls)[key] for key in ("sport", "league", "title")))
        for source_id, title, expected in (("pre", "Capitals Pregame Live", "pregame"), ("post", "Commanders Postgame Live", "postgame"), ("live", "NFL Live", "sports_talk"), ("none", "- NO EVENT STREAMING -", "no_event"), ("separator", "##### FORMULA 1 #####", "no_event")):
            result = self.resolve(source_id, title, "US| NHL PPV")
            self.assertFalse(result["scheduling_eligible"], title)
            self.assertEqual(expected, self.event_row(result)["event_type"])

    def test_motorsport_and_old_dominion_are_safe_without_fake_games(self):
        practice = self.resolve("practice", "Practice 1: Grand Prix of Laguna Seca IndyCar", "US| Apple TV F1 PPV")
        qualifying = self.resolve("qualifying", "Qualifying: Grand Prix of Laguna Seca IndyCar", "US| Apple TV F1 PPV")
        race = self.resolve("race", "Race: Grand Prix of Laguna Seca IndyCar", "US| Apple TV F1 PPV")
        self.assertEqual(("motorsport", "IndyCar", "practice"), tuple(self.event_row(practice)[key] for key in ("sport", "league", "event_type")))
        self.assertEqual(("motorsport", "IndyCar", "qualifying"), tuple(self.event_row(qualifying)[key] for key in ("sport", "league", "event_type")))
        self.assertTrue(race["scheduling_eligible"])
        self.assertEqual("live_race", self.event_row(race)["event_type"])
        odu = self.resolve("odu", "Soccer: Radford vs. Old Dominion", "US| NCAA PPV")
        self.assertIn(("Old Dominion Monarchs", "home"), self.members(odu))
        self.assertEqual("soccer", self.event_row(odu)["sport"])
        self.assertFalse(odu["scheduling_eligible"])
        # An initialism by itself must not manufacture a sport or event.
        ambiguous_odu = self.resolve("odu-short", "ODU", "US| PPV EVENT")
        self.assertFalse(ambiguous_odu["scheduling_eligible"])
        self.assertIsNone(self.event_row(ambiguous_odu)["sport"])

    def test_deterministic_first_keeps_authoritative_results_and_uses_ai_for_gaps(self):
        calls = []

        def requester(_config, _metadata):
            calls.append(True)
            return {"sport": "soccer", "league": "MLS", "program_type": "live_game",
                    "event_type": None, "competition": None, "event_name": None, "sports_related": True,
                    "participants": [{"name": "D.C. United", "role": "away"}, {"name": "Atlanta United FC", "role": "home"}],
                    "language": "en", "start_time_text": None, "network": None, "confidence": .99, "reason": "test"}

        for source_id, title, category in (
            ("angels", "Angels x Nationals", "US| MLB PPV"),
            ("dc", "D.C. vs Atlanta", "US| MLS PPV"),
            ("miami", "MIAMI VS. NASHVILLE", "US| MLS PPV"),
            ("pregame", "Capitals Pregame Live", "US| NHL PPV"),
            ("postgame", "Commanders Postgame Live", "US| NFL PPV"),
        ):
            result = resolve_source_event(self.conn, source="xtream", source_event_id=source_id, data={
                "title": title, "start_utc": START,
                "raw_attributes_json": json.dumps({"provider": "xtream", "category_name": category}),
            }, ai_config=AI_CONFIG, ai_requester=requester)
            self.assertNotEqual("unknown", self.event_row(result)["event_type"], title)
        self.assertEqual([], calls, "strong deterministic results must not spend AI budget")
        self.assertTrue(self.resolve("verify-angels", "Angels x Nationals", "US| MLB PPV")["scheduling_eligible"])

        unresolved = resolve_source_event(self.conn, source="xtream", source_event_id="odu-ai", data={
            "title": "Soccer: Radford vs. Old Dominion", "start_utc": START,
            "raw_attributes_json": json.dumps({"provider": "xtream", "category_name": "US| NCAA PPV"}),
        }, ai_config=AI_CONFIG, ai_requester=requester)
        self.assertEqual(1, len(calls), "unresolved college context remains an AI fallback candidate")
        self.assertFalse(unresolved["scheduling_eligible"])

    def test_ai_cannot_supply_its_own_validation_context(self):
        malicious = {
            "sport": "american_football", "league": "NFL", "program_type": "live_game",
            "event_type": None, "competition": None, "event_name": None, "sports_related": True,
            "participants": [{"name": "Washington Commanders", "role": "away"}, {"name": "New York Giants", "role": "home"}],
            "language": "en", "start_time_text": None, "network": None, "confidence": .99, "reason": "malicious assertion",
        }
        result = resolve_source_event(self.conn, source="xtream", source_event_id="adversarial", data={
            "title": "WASHINGTON @ NEW YORK", "start_utc": START,
            "raw_attributes_json": json.dumps({"provider": "xtream", "category_name": "US| PPV EVENT"}),
        }, ai_config=AI_CONFIG, ai_requester=lambda *_args: malicious)
        event = self.event_row(result)
        metadata = json.loads(event["metadata_json"])
        self.assertFalse(result["scheduling_eligible"])
        self.assertFalse(metadata["provenance"]["independent_context"])
        self.assertEqual("ambiguous participants without league context", result["validation_reason"])


if __name__ == "__main__":
    unittest.main()
