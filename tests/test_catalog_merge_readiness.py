"""Offline merge audit regressions, including names observed in Fruit on 2026-10-10."""
import sys
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from catalog_merge_readiness import audit_catalog, check_db_relationships, compare_names, programmatic_name, programmatic_pair
from capture_catalog_merge_snapshot import make_snapshot


def team(identifier, name, *, league="nba", sport="basketball", merged_into=None):
    return {"id": identifier, "name": name, "sport_id": sport, "league_id": league,
            "state": {"archived": bool(merged_into), "merged_into_id": merged_into}}


def enriched(row, *, mapping_sources=(), alias_count=0, rule_count=0, operator_fields_present=False):
    return {**row, "mapping_sources": list(mapping_sources), "manual_mapping_count": 0,
            "alias_count": alias_count, "rule_count": rule_count,
            "operator_fields_present": operator_fields_present}


class CatalogMergeReadinessTests(unittest.TestCase):
    def test_real_fruit_name_patterns_are_detected_without_authorizing_a_merge(self):
        self.assertEqual("number_prefix", compare_names("Brooklyn Nets", "03 : Brooklyn Nets")["kind"])
        self.assertEqual("parenthetical", compare_names(
            "Atlanta (2026-10-14 19:25:20)",
            "Atlanta (French) (2026-10-14 19:25:30)")["kind"])
        self.assertEqual("different_qualifiers", compare_names(
            "Cleveland Monsters (Away) (2026-10-04 15:00:05)",
            "Cleveland Monsters (Home) (2026-10-04 15:00:00)")["kind"])
        self.assertEqual("different_names", compare_names(
            "New York City Football Club", "San Jose Earthquakes")["kind"])
        self.assertEqual("different_names", compare_names("San Francisco 49ers", "San Francisco ers")["kind"])

    def test_real_fruit_bad_merge_and_numbered_candidate_fail_closed(self):
        catalog = {"teams": [
            team("nyc", "New York City Football Club", sport="soccer", league="mls"),
            team("sj", "San Jose Earthquakes", sport="soccer", league="mls", merged_into="nyc"),
            team("nets", "Brooklyn Nets"),
            team("nets-03", "03 : Brooklyn Nets"),
            team("other", "04 : Brooklyn Nets", league="college"),
        ], "merges": [{"id": 26, "entity_type": "team", "survivor_id": "nyc",
                      "source_id": "sj", "status": "active"}],
                   "provenance": [], "aliases": []}
        result = audit_catalog(catalog)
        self.assertEqual(1, result["summary"]["active_merged_teams"])
        self.assertEqual("different_names", result["existing_merges"][0]["kind"])
        self.assertEqual(1, result["summary"]["unmerged_pattern_candidates"])
        self.assertEqual("number_prefix", result["unmerged_pattern_candidates"][0]["kind"])
        self.assertEqual(0, result["summary"]["automatic_merges_authorized"])
        self.assertFalse(result["summary"]["ready_for_unattended_merging"])

    def test_narrow_numbered_rule_replays_eight_real_fruit_nba_pairs_in_shadow(self):
        observed = [
            ("Brooklyn Nets", "03 : Brooklyn Nets"),
            ("Chicago Bulls", "02 : Chicago Bulls"),
            ("Cleveland Cavaliers", "01 : Cleveland Cavaliers"),
            ("Dallas Mavericks", "01 : Dallas Mavericks"),
            ("Los Angeles Lakers", "06 : Los Angeles Lakers"),
            ("Miami Heat", "02 : Miami Heat"),
            ("New York Knicks", "04 : New York Knicks"),
            ("San Antonio Spurs", "05 : San Antonio Spurs"),
        ]
        rows = []
        for index, (plain, numbered) in enumerate(observed):
            rows.extend((enriched(team(f"base-{index}", plain), mapping_sources=("apple",)),
                         enriched(team(f"variant-{index}", numbered))))
        result = audit_catalog({"teams": rows, "merges": [], "aliases": [], "provenance": []})
        self.assertEqual(8, result["summary"]["shadow_merge_proposals"])
        self.assertEqual(0, result["summary"]["automatic_merges_authorized"])
        self.assertEqual({numbered for _, numbered in observed},
                         {row["variant_name"] for row in result["unmerged_pattern_candidates"]
                          if row["decision"] == "shadow_merge_proposal"})

    def test_shadow_rule_holds_one_word_names_parentheses_and_owned_variants(self):
        catalog = {"teams": [
            enriched(team("cubs", "Cubs", league="mlb", sport="baseball"), mapping_sources=("apple",)),
            enriched(team("numbered-cubs", "02 : Cubs", league="mlb", sport="baseball")),
            enriched(team("yankees", "Yankees", league="mlb", sport="baseball"), mapping_sources=("apple",)),
            enriched(team("numbered-yankees", "02 Yankees", league="mlb", sport="baseball")),
            enriched(team("hawks", "Atlanta Hawks"), mapping_sources=("apple",)),
            enriched(team("french-hawks", "Atlanta Hawks (French)")),
            enriched(team("heat", "Miami Heat"), mapping_sources=("apple",)),
            enriched(team("owned-heat", "03 : Miami Heat"), operator_fields_present=True),
        ], "merges": [], "aliases": [], "provenance": []}
        result = audit_catalog(catalog)
        self.assertEqual(0, result["summary"]["shadow_merge_proposals"])
        reasons = {row["variant_name"]: row["hold_reason"] for row in result["unmerged_pattern_candidates"]}
        self.assertEqual("short_or_ambiguous_base_name", reasons["02 : Cubs"])
        self.assertEqual("not_exact_channel_number_format", reasons["02 Yankees"])
        self.assertEqual("name_normalization_requires_identity_evidence", reasons["Atlanta Hawks (French)"])
        self.assertEqual("operator_edit_present", reasons["03 : Miami Heat"])

    def test_shadow_rule_holds_mapped_or_competing_variants(self):
        catalog = {"teams": [
            enriched(team("nets", "Brooklyn Nets"), mapping_sources=("apple",)),
            enriched(team("mapped", "03 : Brooklyn Nets"), mapping_sources=("xtream",)),
            enriched(team("bulls", "Chicago Bulls"), mapping_sources=("apple",)),
            enriched(team("bulls-one", "01 : Chicago Bulls")),
            enriched(team("bulls-two", "02 : Chicago Bulls")),
        ], "merges": [], "aliases": [], "provenance": []}
        result = audit_catalog(catalog)
        self.assertEqual(0, result["summary"]["shadow_merge_proposals"])
        reasons = {row["variant_name"]: row["hold_reason"] for row in result["unmerged_pattern_candidates"]}
        self.assertEqual("target_not_source_backed_or_variant_has_mapping", reasons["03 : Brooklyn Nets"])
        self.assertEqual("ambiguous_target_or_multiple_variants", reasons["01 : Chicago Bulls"])
        self.assertEqual("ambiguous_target_or_multiple_variants", reasons["02 : Chicago Bulls"])

    def test_name_only_comparison_strips_all_parentheses_and_expands_explicit_abbreviations(self):
        self.assertEqual("carolina panthers", programmatic_name(
            "Carolina Panthers (NFL Football: (Prime Vision) Week 5)")["key"])
        self.assertEqual({"matched": True, "kind": "parenthetical"}, programmatic_pair(
            "Cleveland Monsters (Home)", "Cleveland Monsters (Away)"))
        self.assertEqual({"matched": True, "kind": "abbreviation"}, programmatic_pair(
            "NY Yankees", "New York Yankees"))
        self.assertEqual({"matched": True, "kind": "abbreviation"}, programmatic_pair(
            "#14 MIZ", "#14 Missouri"))
        self.assertFalse(programmatic_pair("LA Galaxy", "LA Lakers")["matched"])
        self.assertFalse(programmatic_pair("Team (unclosed", "Team")["matched"])

    def test_real_style_parenthetical_collision_is_held_and_ids_do_not_enter_name_comparison(self):
        catalog = {"teams": [
            enriched(team("opaque-a", "Cleveland Monsters (Home)")),
            enriched(team("opaque-b", "Cleveland Monsters (Away)")),
            enriched(team("opaque-c", "Cleveland Monsters")),
        ], "merges": [], "aliases": [], "provenance": []}
        result = audit_catalog(catalog)
        self.assertEqual(3, result["summary"]["unmerged_pattern_candidates"])
        self.assertEqual(0, result["summary"]["shadow_merge_proposals"])
        self.assertTrue(all(row["decision"] == "hold_identity_unverified"
                            for row in result["unmerged_pattern_candidates"]))
        renamed_ids = {"opaque-a": "z9", "opaque-b": "a1", "opaque-c": "m5"}
        for row in catalog["teams"]:
            row["id"] = renamed_ids[row["id"]]
        replay = audit_catalog(catalog)
        named_pairs = lambda report: sorted((row["team_name"], row["variant_name"], row["kind"])
                                            for row in report["unmerged_pattern_candidates"])
        self.assertEqual(named_pairs(result), named_pairs(replay))

    def test_cascaded_parent_scope_is_distinguished_from_an_unexplained_mismatch(self):
        catalog = {"teams": [
            team("keep", "Cruzeiro", sport="soccer", league="league-new"),
            team("old", "Cruzeiro", sport="soccer", league="league-old", merged_into="keep"),
        ], "merges": [
            {"id": 1, "entity_type": "league", "survivor_id": "league-new",
             "source_id": "league-old", "status": "active"},
            {"id": 2, "entity_type": "team", "survivor_id": "keep",
             "source_id": "old", "status": "active"},
        ], "provenance": [], "aliases": []}
        result = audit_catalog(catalog)
        self.assertEqual("parent_merge", result["existing_merges"][0]["scope_status"])
        self.assertNotIn("existing merges include unexplained scope mismatches", result["blocking_reasons"])

    def test_snapshot_discards_provider_details_and_preserves_safety_counts(self):
        raw = {"ok": True, "teams": [{**team("base", "Brooklyn Nets"),
                                       "aliases_json": "[]", "secret_payload": "private-marker"}],
               "merges": [], "aliases": [], "provenance": [],
               "source_mappings": [{"entity_type": "team", "canonical_id": "base", "source": "apple",
                                    "source_id": "private-marker", "manual": 0,
                                    "evidence_json": "private-marker"}]}
        result = make_snapshot(raw, {"ok": True, "rules": []})
        self.assertEqual(["apple"], result["teams"][0]["mapping_sources"])
        self.assertNotIn("private-marker", json.dumps(result))

    def test_db_copy_gate_holds_a_variant_with_historical_participants(self):
        rows = [
            enriched(team("nets", "Brooklyn Nets"), mapping_sources=("apple",)),
            enriched(team("nets-numbered", "03 : Brooklyn Nets")),
            enriched(team("bulls", "Chicago Bulls"), mapping_sources=("apple",)),
            enriched(team("bulls-numbered", "02 : Chicago Bulls")),
        ]
        report = audit_catalog({"teams": rows, "merges": [], "aliases": [], "provenance": []})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fruit-copy.db"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE teams(id TEXT, name TEXT, sport_id TEXT, league_id TEXT, aliases_json TEXT);
                CREATE TABLE catalog_entity_state(entity_type TEXT, fruit_id TEXT, archived INTEGER, merged_into_id TEXT, operator_fields_json TEXT);
                CREATE TABLE canonical_event_participants(event_id TEXT, team_id TEXT);
                CREATE TABLE catalog_entity_provenance(entity_type TEXT, fruit_id TEXT);
                CREATE TABLE catalog_aliases(entity_type TEXT, fruit_id TEXT);
                CREATE TABLE source_entity_mappings(entity_type TEXT, canonical_id TEXT, source TEXT, manual INTEGER);
                CREATE TABLE sports_rules(target_type TEXT, target_id TEXT, enabled INTEGER);
            """)
            conn.executemany("INSERT INTO teams VALUES(?,?,?,?,?)",
                             [(row["id"], row["name"], row["sport_id"], row["league_id"], "[]") for row in rows])
            conn.executemany("INSERT INTO source_entity_mappings VALUES('team',?,'apple',0)",
                             [("nets",), ("bulls",)])
            conn.execute("INSERT INTO canonical_event_participants VALUES('old-event','nets-numbered')")
            conn.commit()
            conn.close()
            check_db_relationships(report, path)
        decisions = {row["variant_name"]: row["db_relationship_decision"]
                     for row in report["unmerged_pattern_candidates"]}
        self.assertEqual("hold_existing_relationships", decisions["03 : Brooklyn Nets"])
        self.assertEqual("db_clear", decisions["02 : Chicago Bulls"])
        self.assertEqual(1, report["summary"]["db_clear_shadow_candidates"])


if __name__ == "__main__":
    unittest.main()
