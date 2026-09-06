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

from db.preferences import get_setting, get_settings_schema, save_settings
from server.app import create_app


class AppPrimaryColorPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT)")

    def tearDown(self):
        self.conn.close()

    def test_palette_defaults_to_blue_and_rejects_unknown_values(self):
        self.assertEqual("blue", get_setting(self.conn, "app_primary_color"))
        self.assertTrue(save_settings(self.conn, {"app_primary_color": "purple"}))
        self.assertEqual("purple", get_setting(self.conn, "app_primary_color"))
        self.assertTrue(save_settings(self.conn, {"app_primary_color": "not-a-color"}))
        self.assertEqual("blue", get_setting(self.conn, "app_primary_color"))
        schema = next(item for item in get_settings_schema() if item["key"] == "app_primary_color")
        self.assertEqual(["blue", "red", "green", "purple", "orange"], [item["value"] for item in schema["options"]])


class AppPrimaryColorRenderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "settings.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT)")
        conn.close()
        self.environment = patch.dict(os.environ, {"FRUIT_DB_PATH": str(self.db_path)})
        self.environment.start()
        self.client = create_app().test_client()

    def tearDown(self):
        self.environment.stop()
        self.tempdir.cleanup()

    def test_saved_color_is_rendered_on_shared_page_shell(self):
        default = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('<html lang="en" data-primary-color="blue">', default)
        saved = self.client.post("/api/settings", json={"app_primary_color": "red"})
        self.assertEqual(200, saved.status_code)
        rendered = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('<html lang="en" data-primary-color="red">', rendered)
        self.assertIn('data-primary-color="red"', rendered)


if __name__ == "__main__":
    unittest.main()
