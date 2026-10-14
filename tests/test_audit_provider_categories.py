import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from audit_provider_categories import audit_categories, compare_audits


class CategoryAuditTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE source_event_records(source TEXT, raw_json TEXT)")

    def tearDown(self):
        self.conn.close()

    def add(self, category_id, category_name, title):
        self.conn.execute("INSERT INTO source_event_records VALUES('xtream',?)", (json.dumps({
            "category_id": category_id, "category_name": category_name,
            "epg_title": title, "stream_id": "never-exported",
        }),))

    def test_reports_conflicting_title_evidence_without_promoting_category(self):
        self.add("606", "NHL PPV", "NHL: Capitals at Rangers")
        self.add("606", "NHL PPV", "MLB: Nationals at Phillies")
        result = audit_categories(self.conn)
        category = result["categories"]["606"]
        self.assertEqual(2, category["observations"])
        self.assertEqual(1, category["league_conflicts"])
        self.assertEqual({"MLB": 1, "NHL": 1}, category["title_leagues"])
        self.assertFalse(result["promotion_authorized"])
        self.assertNotIn("never-exported", json.dumps(result))

    def test_comparison_flags_category_name_and_evidence_drift(self):
        self.add("606", "NHL PPV", "NHL: Capitals at Rangers")
        first = audit_categories(self.conn)
        self.conn.execute("DELETE FROM source_event_records")
        self.add("606", "Mixed Sports", "MLB: Nationals at Phillies")
        second = audit_categories(self.conn)
        comparison = compare_audits(first, second)
        self.assertEqual(["606"], comparison["renamed_category_ids"])
        self.assertEqual(["606"], comparison["changed_title_league_evidence"])
        self.assertFalse(comparison["promotion_authorized"])


if __name__ == "__main__":
    unittest.main()
