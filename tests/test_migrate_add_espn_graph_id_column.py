import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import migrate_add_espn_graph_id_column as migrate  # noqa: E402


class MigrateAddEspnGraphIdColumnTest(unittest.TestCase):
    """Regression coverage for the extracted Step 5b migration -- previously
    inlined directly in daily_refresh.py, the one schema change that didn't
    follow the project's migrate_*.py convention.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE playables (event_id TEXT, playable_id TEXT)")

    def test_adds_column_when_missing(self):
        added = migrate.ensure_espn_graph_id_column(self.conn)
        self.assertTrue(added)
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(playables)")]
        self.assertIn("espn_graph_id", cols)

    def test_idempotent_when_already_present(self):
        migrate.ensure_espn_graph_id_column(self.conn)
        added_again = migrate.ensure_espn_graph_id_column(self.conn)
        self.assertFalse(added_again)


if __name__ == "__main__":
    unittest.main()
