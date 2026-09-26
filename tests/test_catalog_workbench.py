import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from catalog_workbench import (_catalog_ai_payload, add_alias, add_proposal, apply_all, detach_merge_relationship,
                               entity_state, merge_details, merge_entities, save_source_mapping,
                               set_entity_fields, undo_merge, effective_visibility,
                               set_visibility_override, set_visibility_bulk, undo_visibility_batch)
from catalog_workbench import merge_selected_entities
from catalog_workbench import run_ai_review
from sports_catalog import apply_catalog_records
from sports_metadata import _upsert_named, ensure_schema, save_rule


class CatalogWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)
        apply_catalog_records(self.conn, [
            {"entity_type": "team", "name": "North Stars", "sport": "Hockey", "league": "NHL", "source": "manual", "operator_confirmed": True},
            {"entity_type": "team", "name": "North Stars Legacy", "sport": "Hockey", "league": "NHL", "source": "manual", "operator_confirmed": True},
        ], dry_run=False)
        self.first, self.second = [row[0] for row in self.conn.execute("SELECT id FROM teams ORDER BY name")]

    def tearDown(self): self.conn.close()

    def test_edit_marks_operator_owned_and_alias_is_manual(self):
        set_entity_fields(self.conn, entity_type="team", fruit_id=self.first, fields={"name": "North Stars FC"})
        add_alias(self.conn, entity_type="team", fruit_id=self.first, alias="NSFC")
        self.assertEqual("North Stars FC", self.conn.execute("SELECT name FROM teams WHERE id=?", (self.first,)).fetchone()[0])
        self.assertEqual("North Stars FC", entity_state(self.conn, "team", self.first)["operator_fields"]["name"])
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM catalog_aliases WHERE fruit_id=? AND alias='NSFC' AND operator_confirmed=1", (self.first,)).fetchone()[0])

    def test_team_merge_and_undo_restore_rule_and_state(self):
        add_alias(self.conn, entity_type="team", fruit_id=self.second, alias="Old Stars")
        save_rule(self.conn, target_type="team", target_id=self.second, policy="PRIORITIZE")
        merge_id = merge_entities(self.conn, entity_type="team", survivor_id=self.first, source_id=self.second)
        self.assertTrue(entity_state(self.conn, "team", self.second)["archived"])
        self.assertEqual(self.first, self.conn.execute("SELECT target_id FROM sports_rules").fetchone()[0])
        undo_merge(self.conn, merge_id)
        self.assertFalse(entity_state(self.conn, "team", self.second)["archived"])
        self.assertEqual(self.second, self.conn.execute("SELECT target_id FROM sports_rules").fetchone()[0])

    def test_checked_team_merge_is_atomic_and_requires_one_scope(self):
        apply_catalog_records(self.conn, [
            {"entity_type": "team", "name": "North Stars Reserve", "sport": "Hockey", "league": "NHL", "source": "manual", "operator_confirmed": True},
            {"entity_type": "team", "name": "North Stars AHL", "sport": "Hockey", "league": "AHL", "source": "manual", "operator_confirmed": True},
        ], dry_run=False)
        reserve = self.conn.execute("SELECT id FROM teams WHERE name='North Stars Reserve'").fetchone()[0]
        ahl = self.conn.execute("SELECT id FROM teams WHERE name='North Stars AHL'").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "same sport and league"):
            merge_selected_entities(self.conn, entity_type="team", survivor_id=self.first,
                                    source_ids=[self.second, ahl])
        self.assertFalse(entity_state(self.conn, "team", self.second)["archived"])
        merge_ids = merge_selected_entities(self.conn, entity_type="team", survivor_id=self.first,
                                            source_ids=[self.second, reserve])
        self.assertEqual(2, len(merge_ids))
        self.assertTrue(entity_state(self.conn, "team", self.second)["archived"])
        self.assertTrue(entity_state(self.conn, "team", reserve)["archived"])

    def test_parent_merge_cascades_exact_child_matches_and_undoes_together(self):
        apply_catalog_records(self.conn, [
            {"entity_type": "team", "name": "Exact Stars", "sport": "Hockey Unified", "league": "NHL", "source": "manual", "operator_confirmed": True},
            {"entity_type": "team", "name": "Exact Stars", "sport": "Hockey Legacy", "league": "NHL", "source": "manual", "operator_confirmed": True},
        ], dry_run=False)
        survivor_sport = self.conn.execute("SELECT id FROM sports WHERE name='Hockey Unified'").fetchone()[0]
        source_sport = self.conn.execute("SELECT id FROM sports WHERE name='Hockey Legacy'").fetchone()[0]
        survivor_league = self.conn.execute("SELECT id FROM leagues WHERE sport_id=? AND name='NHL'", (survivor_sport,)).fetchone()[0]
        source_league = self.conn.execute("SELECT id FROM leagues WHERE sport_id=? AND name='NHL'", (source_sport,)).fetchone()[0]
        survivor_team = self.conn.execute("SELECT id FROM teams WHERE league_id=? AND name='Exact Stars'", (survivor_league,)).fetchone()[0]
        source_team = self.conn.execute("SELECT id FROM teams WHERE league_id=? AND name='Exact Stars'", (source_league,)).fetchone()[0]

        merge_id = merge_entities(self.conn, entity_type="sport", survivor_id=survivor_sport, source_id=source_sport)
        parent_details = merge_details(self.conn, merge_id)
        self.assertEqual(1, len(parent_details["snapshot"]["cascade_merge_ids"]))
        league_merge_id = parent_details["snapshot"]["cascade_merge_ids"][0]
        league_details = merge_details(self.conn, league_merge_id)
        self.assertEqual(1, len(league_details["snapshot"]["cascade_merge_ids"]))
        self.assertTrue(entity_state(self.conn, "sport", source_sport)["archived"])
        self.assertTrue(entity_state(self.conn, "league", source_league)["archived"])
        self.assertTrue(entity_state(self.conn, "team", source_team)["archived"])

        undo_merge(self.conn, merge_id)
        self.assertFalse(entity_state(self.conn, "sport", source_sport)["archived"])
        self.assertFalse(entity_state(self.conn, "league", source_league)["archived"])
        self.assertFalse(entity_state(self.conn, "team", source_team)["archived"])
        self.assertEqual(source_sport, self.conn.execute("SELECT sport_id FROM leagues WHERE id=?", (source_league,)).fetchone()[0])
        self.assertEqual((source_sport, source_league), tuple(self.conn.execute("SELECT sport_id,league_id FROM teams WHERE id=?", (source_team,)).fetchone()))
        self.assertEqual((survivor_sport, survivor_league), tuple(self.conn.execute("SELECT sport_id,league_id FROM teams WHERE id=?", (survivor_team,)).fetchone()))

    def test_apply_all_accepts_safe_alias_and_marks_bad_merge_conflict(self):
        add_proposal(self.conn, run_id=None, entity_type="team", action="alias", target_id=self.first,
                     payload={"alias": "Stars FC"}, evidence={}, confidence=.99)
        add_proposal(self.conn, run_id=None, entity_type="team", action="merge", target_id=None,
                     payload={"survivor_id": self.first, "source_id": self.first}, evidence={}, confidence=.99)
        summary = apply_all(self.conn)
        self.assertEqual(1, summary["accepted"])
        self.assertEqual(1, summary["conflict"])

    def test_ai_review_queues_never_applies_and_accepts_all_entity_tables(self):
        self.conn.execute("CREATE TABLE IF NOT EXISTS user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT)")
        self.conn.execute("INSERT INTO user_preferences VALUES ('setting:local_ai_event_parsing_enabled','true','now')")
        self.conn.execute("INSERT INTO user_preferences VALUES ('setting:local_ai_event_parsing_base_url','http://local','now')")
        self.conn.execute("INSERT INTO user_preferences VALUES ('setting:local_ai_event_parsing_model','test','now')")
        def requester(_config, _payload):
            return {"proposals": [
                {"entity_type": "team", "action": "alias", "target_id": self.first,
                 "payload": {"alias": "AI Stars"}, "confidence": .95, "reason": "fixture"},
                {"entity_type": "team", "action": "merge", "target_id": self.first,
                 "payload": {"name": "North Stars Legacy"}, "confidence": .95, "reason": "missing source ID"},
            ]}
        result = run_ai_review(self.conn, requester=requester)
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM catalog_change_proposals WHERE status='pending'").fetchone()[0])
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) FROM catalog_aliases WHERE alias='AI Stars'").fetchone()[0])

    def test_catalog_ai_prompt_requires_both_merge_ids(self):
        system = _catalog_ai_payload([])["messages"][0]["content"]
        self.assertIn("survivor_id", system)
        self.assertIn("source_id", system)

    def test_manual_name_lock_survives_legacy_import_spelling(self):
        set_entity_fields(self.conn, entity_type="team", fruit_id=self.first, fields={"name": "Northern Stars"})
        league_id = self.conn.execute("SELECT league_id FROM teams WHERE id=?", (self.first,)).fetchone()[0]
        returned = _upsert_named(self.conn, "teams", "North Stars", league_id=league_id)
        self.assertEqual(self.first, returned)
        self.assertEqual("Northern Stars", self.conn.execute("SELECT name FROM teams WHERE id=?", (self.first,)).fetchone()[0])
        self.assertEqual(2, self.conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0])

    def test_manual_source_mapping_and_partial_merge_detach(self):
        save_source_mapping(self.conn, entity_type="team", source="provider", source_id="club-7", canonical_id=self.first)
        self.assertEqual(self.first, self.conn.execute("SELECT canonical_id FROM source_entity_mappings WHERE source='provider'").fetchone()[0])
        add_alias(self.conn, entity_type="team", fruit_id=self.second, alias="Detached Stars")
        merge_id = merge_entities(self.conn, entity_type="team", survivor_id=self.first, source_id=self.second)
        details = merge_details(self.conn, merge_id)
        relationship = "catalog_aliases.fruit_id"
        rowid = next(row[0] for row in details["snapshot"]["moved"][relationship] if row[3] == "Detached Stars")
        self.assertEqual(1, detach_merge_relationship(self.conn, merge_id=merge_id, relationship=relationship, rowids=[rowid]))
        self.assertFalse(entity_state(self.conn, "team", self.second)["archived"])
        self.assertEqual(self.second, self.conn.execute("SELECT fruit_id FROM catalog_aliases WHERE alias='Detached Stars'").fetchone()[0])

    def test_visibility_inherits_and_explicit_normal_overrides_hidden_sport(self):
        sport_id = self.conn.execute("SELECT id FROM sports WHERE name='Hockey'").fetchone()[0]
        league_id = self.conn.execute("SELECT id FROM leagues WHERE name='NHL'").fetchone()[0]
        set_visibility_override(self.conn, entity_type="sport", fruit_id=sport_id, visibility="hidden", reason="not interested")
        self.assertEqual("hidden", effective_visibility(self.conn, "league", league_id)["visibility"])
        self.assertEqual("sport", effective_visibility(self.conn, "league", league_id)["source"])
        set_visibility_override(self.conn, entity_type="league", fruit_id=league_id, visibility="normal")
        self.assertEqual("normal", effective_visibility(self.conn, "league", league_id)["visibility"])
        self.assertEqual("self", effective_visibility(self.conn, "league", league_id)["source"])

    def test_visibility_bulk_undo_does_not_overwrite_later_change(self):
        sport_id = self.conn.execute("SELECT id FROM sports WHERE name='Hockey'").fetchone()[0]
        outcome = set_visibility_bulk(self.conn, entity_type="sport", fruit_ids=[sport_id], visibility="quiet", reason="batch")
        self.assertEqual("quiet", entity_state(self.conn, "sport", sport_id)["visibility_override"])
        undone = undo_visibility_batch(self.conn, outcome["batch_id"])
        self.assertEqual([sport_id], undone["restored"])
        self.assertIsNone(entity_state(self.conn, "sport", sport_id)["visibility_override"])
        outcome = set_visibility_bulk(self.conn, entity_type="sport", fruit_ids=[sport_id], visibility="quiet")
        set_visibility_override(self.conn, entity_type="sport", fruit_id=sport_id, visibility="hidden")
        undone = undo_visibility_batch(self.conn, outcome["batch_id"])
        self.assertEqual([sport_id], undone["conflicts"])


if __name__ == "__main__": unittest.main()
