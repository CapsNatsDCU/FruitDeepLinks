import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from sports_metadata import ensure_schema, resolve_source_event
from local_ai_event_parser import LocalAIConfig
from sports_catalog import apply_catalog_records
from catalog_workbench import delete_classification_mapping, save_classification_mapping


START = "2026-10-11T17:00:00Z"
AI_CONFIG = LocalAIConfig(True, "http://127.0.0.1:11434/v1", "test-model", 1, .8, 20, "deterministic_first")


class SportsInterpretationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def confirm_category(self, label, sport, league):
        apply_catalog_records(self.conn, [
            {"entity_type": "league", "name": league, "sport": sport, "source": "manual"},
        ], dry_run=False)
        league_id = self.conn.execute("SELECT id FROM leagues WHERE name=?", (league,)).fetchone()[0]
        save_classification_mapping(self.conn, source="xtream:category", label=f"{label} | {label}",
                                    target_type="league", canonical_id=league_id)

    def resolve(self, source_id, title, category="", **data):
        return resolve_source_event(self.conn, source="xtream", source_event_id=source_id, data={
            "title": title, "start_utc": START,
            "raw_attributes_json": json.dumps({"provider": "xtream", "category_id": category,
                                                "category_name": category}),
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
        self.confirm_category("US| MLB TEAM PPV", "Baseball", "MLB")
        self.confirm_category("US| NHL TEAM PPV", "Ice hockey", "NHL")
        nationals = self.resolve("nationals", "US: MLB WASHINGTON NATIONALS RAW", "US| MLB TEAM PPV")
        caps = self.resolve("caps", "US: WASHINGTON CAPITALS", "US| NHL TEAM PPV")
        self.assertFalse(nationals["scheduling_eligible"])
        self.assertEqual("single-team permanent feed", nationals["validation_reason"])
        self.assertEqual({("Washington Nationals", "participant")}, self.members(nationals))
        self.assertEqual(("baseball", "MLB", "no_event"), tuple(self.event_row(nationals)[key] for key in ("sport", "league", "event_type")))
        self.assertFalse(caps["scheduling_eligible"])
        self.assertEqual({("Washington Capitals", "participant")}, self.members(caps))
        self.assertEqual(("ice_hockey", "NHL", "no_event"), tuple(self.event_row(caps)[key] for key in ("sport", "league", "event_type")))

    def test_ambiguous_matchup_requires_verified_category_context(self):
        vague = self.resolve("vague", "WASHINGTON @ NEW YORK", "US| PPV EVENT")
        self.assertFalse(vague["scheduling_eligible"])
        self.assertEqual("ambiguous participants without league context", vague["validation_reason"])

        unverified = self.resolve("nhl-unverified", "WSH @ PHI", "US| NHL PPV")
        self.assertFalse(unverified["scheduling_eligible"])
        self.assertIsNone(self.event_row(unverified)["league"])
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0])
        self.confirm_category("US| NHL PPV", "Ice hockey", "NHL")
        self.confirm_category("US| MLB PPV", "Baseball", "MLB")
        nhl = self.resolve("nhl", "WSH @ PHI", "US| NHL PPV")
        mlb = self.resolve("mlb", "WSH @ PHI", "US| MLB PPV")
        self.assertTrue(nhl["scheduling_eligible"])
        self.assertEqual({("Washington Capitals", "away"), ("Philadelphia Flyers", "home")}, self.members(nhl))
        self.assertEqual(("ice_hockey", "NHL", "Washington Capitals at Philadelphia Flyers"), tuple(self.event_row(nhl)[key] for key in ("sport", "league", "title")))
        self.assertTrue(mlb["scheduling_eligible"])
        self.assertEqual({("Washington Nationals", "away"), ("Philadelphia Phillies", "home")}, self.members(mlb))
        self.assertEqual(("baseball", "MLB"), tuple(self.event_row(mlb)[key] for key in ("sport", "league")))

    def test_global_category_mapping_does_not_promote_and_title_conflict_wins(self):
        apply_catalog_records(self.conn, [
            {"entity_type": "league", "name": "NHL", "sport": "Ice hockey", "source": "manual"},
        ], dry_run=False)
        league_id = self.conn.execute("SELECT id FROM leagues WHERE name='NHL'").fetchone()[0]
        save_classification_mapping(self.conn, source="*", label="Mixed PPV",
                                    target_type="league", canonical_id=league_id)
        global_only = self.resolve("global-category", "WSH @ PHI", "Mixed PPV")
        self.assertFalse(global_only["scheduling_eligible"])
        self.assertIsNone(self.event_row(global_only)["league"])
        self.confirm_category("Mixed PPV", "Ice hockey", "NHL")
        conflict = self.resolve("conflicting-category", "MLB: Nationals at Phillies", "Mixed PPV")
        metadata = json.loads(self.event_row(conflict)["metadata_json"])
        self.assertEqual("MLB", self.event_row(conflict)["league"])
        self.assertTrue(metadata["provenance"]["category_mapping_conflict"])

    def test_same_category_name_with_different_id_does_not_inherit_mapping(self):
        self.confirm_category("US| NHL PPV", "Ice hockey", "NHL")
        result = self.resolve("different-category-id", "WSH @ PHI", "US| NHL PPV",
                              raw_attributes_json=json.dumps({
                                  "provider": "xtream", "category_id": "other",
                                  "category_name": "US| NHL PPV",
                              }))
        self.assertFalse(result["scheduling_eligible"])

    def test_category_name_change_does_not_inherit_mapping(self):
        self.confirm_category("NHL-PPV", "Ice hockey", "NHL")
        result = self.resolve("renamed-category", "WSH @ PHI", "NHL PPV",
                              raw_attributes_json=json.dumps({
                                  "provider": "xtream", "category_id": "NHL-PPV",
                                  "category_name": "NHL PPV",
                              }))
        self.assertFalse(result["scheduling_eligible"])

    def test_opaque_provider_sport_and_league_ids_are_not_classification(self):
        data = {"sport_name": "123", "league_name": "Q456",
                "competitors": [{"name": "Washington Capitals", "homeAway": "away"},
                                {"name": "New York Rangers", "homeAway": "home"}]}
        unverified = self.resolve("opaque-classification", "Capitals at Rangers", "Mixed PPV", **data)
        row = self.event_row(unverified)
        self.assertFalse(unverified["scheduling_eligible"])
        self.assertIsNone(row["sport"])
        self.assertIsNone(row["league"])
        self.assertFalse(json.loads(row["metadata_json"])["provenance"]["independent_context"])
        self.confirm_category("Mixed PPV", "Ice hockey", "NHL")
        verified = self.resolve("mapped-classification", "Capitals at Rangers", "Mixed PPV", **data)
        self.assertTrue(verified["scheduling_eligible"])
        self.assertEqual("NHL", self.event_row(verified)["league"])

    def test_revoked_category_mapping_rechecks_prior_resolution(self):
        self.confirm_category("US| NHL PPV", "Ice hockey", "NHL")
        first = self.resolve("revoked", "WSH @ PHI", "US| NHL PPV")
        self.assertTrue(first["scheduling_eligible"])
        delete_classification_mapping(self.conn, source="xtream:category",
                                      label="US| NHL PPV | US| NHL PPV")
        second = self.resolve("revoked", "WSH @ PHI", "US| NHL PPV")
        self.assertFalse(second["scheduling_eligible"])
        self.assertNotEqual(first["canonical_event_id"], second["canonical_event_id"])
        old = self.event_row(first)
        self.assertEqual("superseded", old["status"])
        self.assertFalse(json.loads(old["metadata_json"])["scheduling_eligible"])

    def test_changed_category_mapping_rechecks_prior_scope(self):
        self.confirm_category("Mixed PPV", "Ice hockey", "NHL")
        first = self.resolve("remapped", "WSH @ PHI", "Mixed PPV")
        self.assertEqual("NHL", self.event_row(first)["league"])
        self.confirm_category("Mixed PPV", "Baseball", "MLB")
        second = self.resolve("remapped", "WSH @ PHI", "Mixed PPV")
        self.assertEqual("MLB", self.event_row(second)["league"])
        self.assertNotEqual(first["canonical_event_id"], second["canonical_event_id"])
        self.assertEqual("superseded", self.event_row(first)["status"])

    def test_at_symbol_matchup_is_not_misclassified_by_live_description(self):
        result = self.resolve(
            "epg-live",
            "NFL | Commanders @ Ravens",
            raw_attributes_json=json.dumps({
                "provider": "xtream",
                "category_name": "US| NFL PPV",
                "epg_description": "NFL live coverage",
            }),
        )

        self.assertTrue(result["scheduling_eligible"])
        self.assertEqual("live_game", self.event_row(result)["event_type"])

    def test_normalized_display_names_and_non_game_programming(self):
        self.confirm_category("US| NFL PPV", "American football", "NFL")
        self.confirm_category("US| MLS PPV", "Soccer", "MLS")
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
        self.assertEqual(set(), self.members(odu))
        self.assertEqual("soccer", self.event_row(odu)["sport"])
        self.assertFalse(odu["scheduling_eligible"])
        # An initialism by itself must not manufacture a sport or event.
        ambiguous_odu = self.resolve("odu-short", "ODU", "US| PPV EVENT")
        self.assertFalse(ambiguous_odu["scheduling_eligible"])
        self.assertIsNone(self.event_row(ambiguous_odu)["sport"])

    def test_deterministic_first_keeps_authoritative_results_and_uses_ai_for_gaps(self):
        for label, sport, league in (("US| MLB PPV", "Baseball", "MLB"),
                                     ("US| MLS PPV", "Soccer", "MLS"),
                                     ("US| NHL PPV", "Ice hockey", "NHL"),
                                     ("US| NFL PPV", "American football", "NFL")):
            self.confirm_category(label, sport, league)
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
                "raw_attributes_json": json.dumps({"provider": "xtream", "category_id": category,
                                                    "category_name": category}),
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
