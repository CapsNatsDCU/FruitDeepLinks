import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from catalog_workbench import (add_alias, add_proposal, apply_all, detach_merge_relationship,
                               entity_state, merge_details, merge_entities, save_source_mapping,
                               set_entity_fields, undo_merge)
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
            return {"proposals": [{"entity_type": "team", "action": "alias", "target_id": self.first,
                                   "payload": {"alias": "AI Stars"}, "confidence": .95, "reason": "fixture"}]}
        result = run_ai_review(self.conn, requester=requester)
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM catalog_change_proposals WHERE status='pending'").fetchone()[0])
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) FROM catalog_aliases WHERE alias='AI Stars'").fetchone()[0])

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


if __name__ == "__main__": unittest.main()
