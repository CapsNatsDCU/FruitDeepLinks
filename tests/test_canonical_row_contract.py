import os
import sqlite3
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

from sports_metadata import (applicable_rule, coverage, ensure_schema,
                             resolve_source_event, save_rule)


UTC = timezone.utc


class CanonicalRowContractTests(unittest.TestCase):
    def _canonical_event(self, conn, source_id="source-event"):
        return resolve_source_event(conn, source="xtream", source_event_id=source_id, data={
            "title": "Washington Capitals at New York Rangers",
            "sport_name": "Hockey",
            "league_name": "NHL",
            "event_type": "regular",
            "start_utc": "2026-10-11T17:00:00Z",
            "competitors": [
                {"name": "Washington Capitals", "homeAway": "away"},
                {"name": "New York Rangers", "homeAway": "home"},
            ],
        })["canonical_event_id"]

    def test_applicable_rule_has_one_mapping_contract_for_row_and_tuple_connections(self):
        for factory in (sqlite3.Row, None):
            with self.subTest(row_factory=factory):
                conn = sqlite3.connect(":memory:")
                conn.row_factory = factory
                try:
                    ensure_schema(conn)
                    canonical_id = self._canonical_event(conn)

                    # A canonical record with no rule must remain the normal
                    # legacy-compatible scheduling case.
                    self.assertEqual("NORMAL", applicable_rule(conn, canonical_id)["policy"])

                    save_rule(conn, target_type="event", target_id=canonical_id,
                              policy="ALWAYS_SCHEDULE")
                    matching = applicable_rule(conn, canonical_id)
                    self.assertEqual("ALWAYS_SCHEDULE", matching["policy"])
                    self.assertEqual(10000, matching["priority"])
                    # Coverage bulk-loads events, participants, rules, source
                    # mappings, and decisions.  It used the same unsafe
                    # dict(tuple) pattern and must share this contract too.
                    self.assertEqual(
                        canonical_id,
                        next(item for item in coverage(conn, days=90)
                             if item["canonical_event_id"] == canonical_id)["canonical_event_id"],
                    )

                    # A provider/legacy ID is not a canonical ID and must not
                    # be mistaken for a tuple mapping while checking rules.
                    self.assertEqual(
                        "no_canonical_event",
                        applicable_rule(conn, "legacy-unmapped-event-id")["reason"],
                    )
                finally:
                    conn.close()

    def test_manual_lane_builder_cli_handles_default_tuple_rows_through_sync_rules_and_scheduling(self):
        """Regression for dict(event) in the actual manual-refresh command path."""
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "fruit_events.db"
            conn = sqlite3.connect(db_path)  # Same default tuple setup as fruit_build_lanes.py.
            try:
                now = datetime.now(UTC).replace(microsecond=0)
                start = now + timedelta(hours=1)
                end = start + timedelta(hours=3)
                conn.executescript("""
                    CREATE TABLE events (
                        id TEXT PRIMARY KEY, pvid TEXT, slug TEXT, title TEXT,
                        channel_name TEXT, start_utc TEXT, end_utc TEXT,
                        raw_attributes_json TEXT, genres_json TEXT,
                        classification_json TEXT, channel_provider_id TEXT
                    );
                """)
                conn.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "xtream:manual-refresh-event-12345", "real-pvid", None,
                        "Washington Capitals at New York Rangers", "NHL",
                        start.isoformat().replace("+00:00", "Z"),
                        end.isoformat().replace("+00:00", "Z"),
                        '{"provider":"xtream","sport_name":"Hockey","league_name":"NHL",'
                        '"competitors":[{"name":"Washington Capitals","homeAway":"away"},'
                        '{"name":"New York Rangers","homeAway":"home"}]}',
                        "[]", "[]", "xtream",
                    ),
                )
                ensure_schema(conn)
                canonical_id = self._canonical_event(conn, "xtream:manual-refresh-event-12345")
                save_rule(conn, target_type="event", target_id=canonical_id,
                          policy="ALWAYS_SCHEDULE")
                conn.commit()
            finally:
                conn.close()

            completed = subprocess.run(
                [sys.executable, str(REPO_ROOT / "bin" / "fruit_build_lanes.py"),
                 "--db", str(db_path), "--lanes", "2", "--days-ahead", "2",
                 "--canonical-ai-mode", "disabled"],
                cwd=REPO_ROOT,
                env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "bin")},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertIn("Lane planning complete", completed.stdout)

            verify = sqlite3.connect(db_path)
            try:
                scheduled = verify.execute(
                    "SELECT event_id FROM lane_events WHERE is_placeholder=0"
                ).fetchall()
                self.assertEqual([("xtream:manual-refresh-event-12345",)], scheduled)
                self.assertGreater(
                    verify.execute("SELECT COUNT(*) FROM lanes").fetchone()[0], 0
                )
            finally:
                verify.close()


if __name__ == "__main__":
    unittest.main()
