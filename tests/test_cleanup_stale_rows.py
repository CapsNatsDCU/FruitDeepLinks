import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import cleanup_stale_rows  # noqa: E402


class CleanupStaleRowsTest(unittest.TestCase):
    """Regression coverage for the consolidated stale-row cleanup, replacing
    three hand-copied inline blocks that used to live in daily_refresh.py
    (fruit_events.db events, apple_events.db apple_events, espn_graph.db
    events) -- same shape every time, now one script.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_path = str(Path(self.tmpdir.name) / "test.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute(
            """
            CREATE TABLE events (
                id TEXT PRIMARY KEY,
                end_utc TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE playables (
                event_id TEXT REFERENCES events(id) ON DELETE CASCADE,
                playable_id TEXT
            )
            """
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_deletes_only_matching_rows(self):
        self.conn.execute("INSERT INTO events (id, end_utc) VALUES ('old', datetime('now', '-5 days'))")
        self.conn.execute("INSERT INTO events (id, end_utc) VALUES ('new', datetime('now', '+5 days'))")
        self.conn.commit()

        stale_count = cleanup_stale_rows.cleanup(
            self.db_path, "events", "end_utc < datetime('now', '-1 day')", "test", cascade=False,
        )
        self.assertEqual(stale_count, 1)

        remaining = [r[0] for r in self.conn.execute("SELECT id FROM events")]
        self.assertEqual(remaining, ["new"])

    def test_no_stale_rows_is_a_clean_noop(self):
        self.conn.execute("INSERT INTO events (id, end_utc) VALUES ('new', datetime('now', '+5 days'))")
        self.conn.commit()

        stale_count = cleanup_stale_rows.cleanup(
            self.db_path, "events", "end_utc < datetime('now', '-1 day')", "test", cascade=False,
        )
        self.assertEqual(stale_count, 0)
        remaining = [r[0] for r in self.conn.execute("SELECT id FROM events")]
        self.assertEqual(remaining, ["new"])

    def test_cascade_flag_removes_children(self):
        self.conn.execute("INSERT INTO events (id, end_utc) VALUES ('old', datetime('now', '-5 days'))")
        self.conn.execute("INSERT INTO playables (event_id, playable_id) VALUES ('old', 'p1')")
        self.conn.commit()

        cleanup_stale_rows.cleanup(
            self.db_path, "events", "end_utc < datetime('now', '-1 day')", "test", cascade=True,
        )

        remaining_playables = self.conn.execute("SELECT * FROM playables").fetchall()
        self.assertEqual(remaining_playables, [], "cascade=True must let ON DELETE CASCADE strand no children")

    def test_without_cascade_children_are_stranded(self):
        # Documents the actual (opt-in) behavior: without --cascade, SQLite's
        # per-connection default (foreign_keys=OFF) means deleting the parent
        # leaves the child row behind -- this is why fruit_events.db and
        # espn_graph.db's cleanups pass --cascade and apple_events.db's
        # (no child table) doesn't need to.
        self.conn.execute("INSERT INTO events (id, end_utc) VALUES ('old', datetime('now', '-5 days'))")
        self.conn.execute("INSERT INTO playables (event_id, playable_id) VALUES ('old', 'p1')")
        self.conn.commit()

        cleanup_stale_rows.cleanup(
            self.db_path, "events", "end_utc < datetime('now', '-1 day')", "test", cascade=False,
        )

        remaining_playables = self.conn.execute("SELECT * FROM playables").fetchall()
        self.assertEqual(len(remaining_playables), 1)


if __name__ == "__main__":
    unittest.main()
