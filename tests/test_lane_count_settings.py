import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


BIN = Path(__file__).resolve().parents[1] / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

from db.preferences import get_setting, get_settings_schema
from server.app import create_app
from server import refresh


class LaneCountSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fruit.db"
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT)")
        self.environment = patch.dict(os.environ, {"FRUIT_DB_PATH": str(self.path), "FRUIT_LANES": "50"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = create_app().test_client()

    def test_lane_count_is_bounded_and_saved_through_settings_api(self):
        schema = next(item for item in get_settings_schema() if item["key"] == "num_lanes")
        self.assertEqual((1, 750, 1), (schema["min"], schema["max"], schema["step"]))
        response = self.client.post("/api/settings", json={"num_lanes": 7})
        self.assertEqual(200, response.status_code)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(7, get_setting(conn, "num_lanes"))
        for invalid in (0, 751, True, "7"):
            with self.subTest(value=invalid):
                response = self.client.post("/api/settings", json={"num_lanes": invalid})
                self.assertEqual(400, response.status_code)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(7, get_setting(conn, "num_lanes"))

    def test_apply_filters_passes_saved_lane_count_to_lane_builder(self):
        self.assertEqual(200, self.client.post("/api/settings", json={"num_lanes": 7}).status_code)
        process = Mock(returncode=0, stdout=[])
        refresh.refresh_status["running"] = False
        with patch.object(refresh.cfg, "DB_PATH", self.path), \
             patch("server.refresh.subprocess.Popen", return_value=process) as popen:
            refresh.run_apply_filters()
        first_command = popen.call_args_list[0].args[0]
        self.assertEqual("fruit_build_lanes.py", Path(first_command[2]).name)
        self.assertEqual("7", first_command[first_command.index("--lanes") + 1])
        self.assertEqual("success", refresh.refresh_status["last_status"])


if __name__ == "__main__":
    unittest.main()
