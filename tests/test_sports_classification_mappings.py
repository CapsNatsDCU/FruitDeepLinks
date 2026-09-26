import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from catalog_workbench import classification_mapping_for_label, save_classification_mapping
from sports_catalog import apply_catalog_records
from sports_metadata import ensure_schema, resolve_source_event


class ClassificationMappingResolverTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)
        apply_catalog_records(self.conn, [
            {"entity_type": "sport", "name": "american_football", "source": "test"},
            {"entity_type": "league", "name": "NFL", "sport": "american_football", "source": "test"},
        ], dry_run=False)
        self.sport_id = self.conn.execute("SELECT id FROM sports WHERE name='american_football'").fetchone()[0]
        self.league_id = self.conn.execute("SELECT id FROM leagues WHERE name='NFL'").fetchone()[0]

    def tearDown(self):
        self.conn.close()

    def test_provider_label_promotes_a_mislabeled_sport_to_its_league(self):
        save_classification_mapping(self.conn, source="xtream", label="NFL Network Schedule",
                                    target_type="league", canonical_id=self.league_id)
        result = resolve_source_event(self.conn, source="xtream", source_event_id="one", data={
            "title": "Commanders at Eagles", "start_utc": "2026-10-11T17:00:00Z",
            "sport_name": "NFL Network Schedule",
            "competitors": [
                {"name": "Washington Commanders", "homeAway": "away"},
                {"name": "Philadelphia Eagles", "homeAway": "home"},
            ],
            "raw_attributes_json": json.dumps({"provider": "xtream"}),
        }, ai_mode="disabled")
        event = self.conn.execute(
            "SELECT s.name,l.name,ce.metadata_json FROM canonical_events ce "
            "LEFT JOIN sports s ON s.id=ce.sport_id LEFT JOIN leagues l ON l.id=ce.league_id WHERE ce.id=?",
            (result["canonical_event_id"],),
        ).fetchone()
        self.assertEqual(("american_football", "NFL"), tuple(event[:2]))
        self.assertEqual("NFL Network Schedule", json.loads(event[2])["provenance"]["classification_mappings"][0]["label"])

    def test_provider_specific_mapping_beats_the_global_fallback(self):
        save_classification_mapping(self.conn, source="*", label="Gridiron", target_type="sport", canonical_id=self.sport_id)
        save_classification_mapping(self.conn, source="xtream", label="Gridiron", target_type="league", canonical_id=self.league_id)
        self.assertEqual("league", classification_mapping_for_label(self.conn, source="xtream", label="Gridiron")["target_type"])
        self.assertEqual("sport", classification_mapping_for_label(self.conn, source="apple", label="Gridiron")["target_type"])


class ClassificationMappingCatalogApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "settings.db"
        sqlite3.connect(self.db_path).close()
        self.env = patch.dict(os.environ, {"FRUIT_DB_PATH": str(self.db_path)})
        self.env.start()
        from server.app import create_app
        self.client = create_app().test_client()

    def tearDown(self):
        self.env.stop()
        self.tempdir.cleanup()

    def test_catalog_workbench_exposes_editable_classification_mappings(self):
        with sqlite3.connect(self.db_path) as conn:
            ensure_schema(conn)
            apply_catalog_records(conn, [
                {"entity_type": "sport", "name": "baseball", "source": "test"},
                {"entity_type": "league", "name": "MLB", "sport": "baseball", "source": "test"},
            ], dry_run=False)
            league_id = conn.execute("SELECT id FROM leagues WHERE name='MLB'").fetchone()[0]
        saved = self.client.put("/api/sports/catalog/classification-mappings", json={
            "source": "xtream", "label": "Major League Baseball", "target_type": "league", "canonical_id": league_id,
        })
        self.assertEqual(200, saved.status_code)
        mapping = self.client.get("/api/sports/catalog").get_json()["classification_mappings"][0]
        self.assertEqual(("xtream", "Major League Baseball", "MLB"),
                         (mapping["source"], mapping["label"], "MLB"))
        page = self.client.get("/sports-catalog").get_data(as_text=True)
        self.assertIn("Correct provider sport labels", page)
        self.assertNotIn("Mapping JSON", page)


if __name__ == "__main__":
    unittest.main()
