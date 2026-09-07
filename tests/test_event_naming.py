import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from event_naming import build_normalized_name, ensure_normalized_name_column, programming_name


class EventNamingTest(unittest.TestCase):
    def test_explicit_home_away_roles_use_conventional_road_at_home_order(self):
        event = {
            "title": "Noisy provider title",
            "classification_json": json.dumps([{"type": "league", "value": "NHL"}]),
            "raw_attributes_json": json.dumps({
                "competitors": [
                    {"name": "Montreal Canadiens", "home": True},
                    {"name": "Toronto Maple Leafs", "home": False},
                ]
            }),
        }
        self.assertEqual("[NHL] Toronto Maple Leafs @ Montreal Canadiens", build_normalized_name(event))

    def test_untyped_title_order_is_preserved_and_pipe_labels_are_supported(self):
        event = {"title": "NHL | Capitals @ Lightning | 2026-09-04 7:00 PM"}
        self.assertEqual("[NHL] Capitals @ Lightning", build_normalized_name(event))
        event["title"] = "NHL | 05 - 8/28 6pm Capitals at Lightning"
        self.assertEqual("[NHL] Capitals @ Lightning", build_normalized_name(event))

    def test_override_wins_and_empty_override_falls_back(self):
        event = {"title": "NHL | Capitals @ Lightning", "normalized_name": "[NHL] Washington @ Tampa Bay"}
        self.assertEqual("[NHL] Washington @ Tampa Bay", programming_name(event))
        event["normalized_name"] = "  "
        self.assertEqual("[NHL] Capitals @ Lightning", programming_name(event))

    def test_automatic_name_includes_broadcast_label(self):
        event = {
            "title": "NHL | Capitals @ Lightning",
            "classification_json": json.dumps([
                {"type": "league", "value": "NHL"},
            ]),
            "broadcast_name": "ESPN+",
        }
        self.assertEqual(
            "[NHL] Capitals @ Lightning (ESPN+)",
            programming_name(event),
        )

    def test_manual_override_does_not_get_broadcast_suffix(self):
        event = {
            "normalized_name": "Game title chosen by operator",
            "title": "NHL | Capitals @ Lightning",
            "broadcast_name": "ESPN+",
        }
        self.assertEqual("Game title chosen by operator", programming_name(event))

    def test_migration_is_idempotent(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE events (id TEXT PRIMARY KEY, title TEXT)")
        self.assertTrue(ensure_normalized_name_column(conn))
        self.assertFalse(ensure_normalized_name_column(conn))
        self.assertIn("normalized_name", {row[1] for row in conn.execute("PRAGMA table_info(events)")})


if __name__ == "__main__":
    unittest.main()
