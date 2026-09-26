import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from local_ai_event_parser import active_failures
from sports_metadata import ensure_schema, retry_failed_local_ai, sync_legacy_events


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        interpretation = {
            "sport": "Ice hockey", "league": "NHL", "event_type": "event",
            "competition": None, "participants": [], "language": "en",
            "start_time_text": None, "network": None, "confidence": .9, "reason": "fixture",
        }
        return json.dumps({"choices": [{"message": {"content": json.dumps(interpretation)}}]}).encode()


class CanonicalSyncModesTests(unittest.TestCase):
    def build(self, count=30):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        conn.executescript("""
          CREATE TABLE events (
            id TEXT PRIMARY KEY, title TEXT, start_utc TEXT, end_utc TEXT,
            raw_attributes_json TEXT, classification_json TEXT, channel_provider_id TEXT
          );
          CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT);
        """)
        settings = {
            "local_ai_event_parsing_enabled": True,
            "local_ai_event_parsing_base_url": "http://local.test/v1",
            "local_ai_event_parsing_model": "test-model",
            "local_ai_event_parsing_max_requests_per_refresh": 2,
            "local_ai_event_parsing_timeout_seconds": 60,
        }
        conn.executemany("INSERT INTO user_preferences(key,value) VALUES(?,?)",
                         [(f"setting:{key}", json.dumps(value)) for key, value in settings.items()])
        conn.executemany("INSERT INTO events VALUES(?,?,?,?,?,?,?)", [
            (f"xtream:{index}", f"Unstructured sports title {index}", "2026-10-11T17:00:00Z",
             "2026-10-11T20:00:00Z", '{"provider":"xtream"}', '[]', "xtream")
            for index in range(count)
        ])
        return conn

    def test_disabled_bounded_and_unlimited_are_real_modes(self):
        conn = self.build()
        try:
            with patch("local_ai_event_parser.urlopen", return_value=_Response()) as request:
                disabled = sync_legacy_events(conn, ai_mode="disabled")
                self.assertEqual(0, request.call_count)
                self.assertEqual(0, disabled["requests"])

                bounded = sync_legacy_events(conn, ai_mode="bounded")
                self.assertEqual(2, request.call_count)
                self.assertEqual(2, bounded["requests"])
                self.assertEqual(28, bounded["budget_exhausted"])

                unlimited = sync_legacy_events(conn, ai_mode="unlimited")
                self.assertEqual(30, request.call_count)
                self.assertEqual(28, unlimited["requests"])
                self.assertEqual(2, unlimited["cache_hits"])
        finally:
            conn.close()

    def test_timeout_configuration_accepts_sixty_seconds(self):
        conn = self.build(1)
        try:
            with patch("local_ai_event_parser.urlopen", side_effect=TimeoutError) as request:
                summary = sync_legacy_events(conn, ai_mode="unlimited")
            self.assertEqual(2, request.call_count)
            self.assertEqual(1, summary["timeouts"])
            self.assertEqual(1, summary["transport_failures"])
        finally:
            conn.close()

    def test_operator_retry_reprocesses_only_logged_failures(self):
        conn = self.build(2)
        try:
            with patch("local_ai_event_parser.urlopen", side_effect=TimeoutError):
                sync_legacy_events(conn, ai_mode="unlimited")
            self.assertEqual(2, len(active_failures(conn)))
            with patch("local_ai_event_parser.urlopen", return_value=_Response()) as request:
                result = retry_failed_local_ai(conn)
            self.assertEqual(2, result["retry_targets"])
            self.assertEqual(2, request.call_count)
            self.assertEqual([], active_failures(conn))
        finally:
            conn.close()

    def test_deterministic_and_ai_work_are_reported_as_separate_passes(self):
        conn = self.build(1)
        reports = []
        try:
            with patch("local_ai_event_parser.urlopen", return_value=_Response()):
                summary = sync_legacy_events(
                    conn, ai_mode="bounded", progress_callback=lambda **payload: reports.append(payload),
                )
            self.assertEqual("deterministic", reports[0]["pass_name"])
            self.assertEqual("running", reports[0]["status"])
            self.assertEqual("ai", reports[-1]["pass_name"])
            self.assertEqual("complete", reports[-1]["status"])
            self.assertEqual(1, summary["deterministic_pass"]["resolved"])
            self.assertEqual(1, summary["ai_pass"]["requests"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
