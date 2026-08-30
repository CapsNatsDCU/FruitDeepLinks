import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

from server.app import create_app  # noqa: E402
from server.logging_setup import get_recent_logs  # noqa: E402
from server.services.xtream_persistent import (  # noqa: E402
    ChannelNumberConflict,
    DuplicatePersistentChannel,
    create_channel,
    delete_channel,
    ensure_schema,
    get_channel,
    list_channels,
    page_streams,
    reconcile_channels,
    render_m3u,
    render_xmltv,
    update_channel,
)
from xtream_ingest import (  # noqa: E402
    XtreamConfig,
    ensure_schema as ensure_ingest_schema,
    ingest_payload,
    normalize_stream,
)


FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "xtream_mlb_team_ppv.json").read_text(encoding="utf-8")
)
NATIONALS = FIXTURE["streams"][0]


def xtream_config():
    return XtreamConfig(
        enabled=True,
        server_url="http://provider.example:8080",
        username="demo user",
        password="secret/pass",
        category_ids=("410",),
        timezone_name="America/New_York",
        default_duration_minutes=180,
    )


class PersistentChannelServiceTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def add(self, **overrides):
        values = {
            "category_id": "410",
            "category_name": "MLB TEAM PPV",
            "channel_number": "22",
            "display_name": "Washington Nationals",
            "favorite_team": "Washington Nationals",
        }
        values.update(overrides)
        return create_channel(self.conn, NATIONALS, **values)

    def test_crud_and_optional_favorite_team_association(self):
        created = self.add()
        self.assertEqual("1904224", created["stream_id"])
        self.assertEqual("Washington Nationals", created["favorite_team"])
        updated = update_channel(self.conn, created["id"], {
            "display_name": "Nationals Home Feed",
            "channel_number": "22.1",
            "guide_id": "custom.nationals",
            "notes": "Preferred home feed",
            "enabled": False,
        })
        self.assertEqual("Nationals Home Feed", updated["display_name"])
        self.assertEqual("22.1", updated["channel_number"])
        self.assertFalse(updated["enabled"])
        self.assertTrue(delete_channel(self.conn, created["id"]))
        self.assertEqual([], list_channels(self.conn))

    def test_duplicate_stream_and_channel_number_are_rejected(self):
        self.add()
        with self.assertRaises(DuplicatePersistentChannel):
            self.add(channel_number="23")
        with self.assertRaises(ChannelNumberConflict):
            create_channel(
                self.conn,
                FIXTURE["streams"][1],
                category_id="410",
                category_name="MLB TEAM PPV",
                channel_number="22.0",
            )

    def test_static_stream_is_valid_persistent_but_remains_invalid_dynamic_event(self):
        channel = self.add()
        self.assertIsNotNone(channel)
        self.assertIsNone(
            normalize_stream(NATIONALS, "410", "MLB TEAM PPV", xtream_config())
        )

    def test_browse_filter_and_paging(self):
        page = page_streams(FIXTURE["streams"], "washington", page=1, page_size=25)
        self.assertEqual(1, page["total"])
        self.assertEqual("1904224", page["items"][0]["stream_id"])
        self.assertEqual("mlb.nationals", page["items"][0]["epg_channel_id"])

    def test_m3u_and_xmltv_are_stable_and_never_contain_credentials(self):
        self.add(guide_id="mlb.nationals")
        m3u = render_m3u(self.conn, "http://fruit.local:6655")
        xml = render_xmltv(self.conn).decode("utf-8")
        self.assertIn('tvg-id="mlb.nationals"', m3u)
        self.assertIn('tvg-chno="22"', m3u)
        self.assertIn("http://fruit.local:6655/xtream/channel/1/stream", m3u)
        self.assertIn("Washington Nationals", xml)
        self.assertEqual("mlb.nationals", ET.fromstring(xml).find("channel").attrib["id"])
        for output in (m3u, xml):
            self.assertNotIn("demo user", output)
            self.assertNotIn("secret", output)
            self.assertNotIn("/live/", output)
        # No fabricated programme data is emitted when no schedule exists.
        self.assertNotIn("<programme", xml)

    def test_disabled_channel_is_excluded_from_both_exports(self):
        self.add(enabled=False)
        self.assertNotIn("Washington Nationals", render_m3u(self.conn, "http://fruit"))
        self.assertNotIn("Washington Nationals", render_xmltv(self.conn).decode())

    def test_missing_stream_is_retained_and_marked_unavailable(self):
        channel = self.add()
        result = reconcile_channels(self.conn, {"410": FIXTURE["streams"][1:]})
        current = get_channel(self.conn, channel["id"])
        self.assertEqual(1, result["persistent_unavailable"])
        self.assertEqual("unavailable", current["availability_status"])
        self.assertEqual(1, len(list_channels(self.conn)))

    def test_changed_stream_id_reconciles_one_exact_normalized_name(self):
        channel = self.add()
        replacement = dict(NATIONALS, stream_id=991122)
        result = reconcile_channels(self.conn, {"410": [replacement]})
        current = get_channel(self.conn, channel["id"])
        self.assertEqual(1, result["persistent_reconciled"])
        self.assertEqual("991122", current["stream_id"])
        self.assertEqual("available", current["availability_status"])

    def test_ambiguous_replacement_never_auto_selects(self):
        channel = self.add()
        first = dict(NATIONALS, stream_id=991122)
        second = dict(NATIONALS, stream_id=991123)
        result = reconcile_channels(self.conn, {"410": [first, second]})
        current = get_channel(self.conn, channel["id"])
        self.assertEqual(0, result["persistent_reconciled"])
        self.assertEqual("1904224", current["stream_id"])
        self.assertEqual("needs_attention", current["availability_status"])

    def test_dynamic_ingest_and_non_xtream_rows_remain_independent(self):
        self.add()
        ensure_ingest_schema(self.conn)
        self.conn.execute(
            "INSERT INTO events(id,title,raw_attributes_json) VALUES('other','Other','{}')"
        )
        self.conn.execute(
            "INSERT INTO playables(event_id,playable_id,provider,logical_service) "
            "VALUES('other','other-playable','peacock','peacock')"
        )
        self.conn.commit()
        result = ingest_payload(
            self.conn,
            [FIXTURE["category"]],
            {"410": FIXTURE["streams"]},
            xtream_config(),
        )
        self.assertEqual(0, result["normalized"])
        self.assertEqual(1, result["persistent_available"])
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM playables WHERE provider='peacock'").fetchone()[0])


class FakeXtreamClient:
    def __init__(self, *args, **kwargs):
        pass

    def get_live_categories(self):
        return [FIXTURE["category"], {"category_id": "999", "category_name": "Not configured"}]

    def get_live_streams(self, category_id):
        if str(category_id) != "410":
            raise AssertionError("Only the explicitly opened category may be fetched")
        return list(FIXTURE["streams"])


class PersistentChannelApiWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "fruit.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, updated_utc TEXT)"
        )
        settings = {
            "xtream_enabled": True,
            "xtream_server_url": "http://provider.example:8080",
            "xtream_category_ids": "410",
            "xtream_timezone": "America/New_York",
            "xtream_default_duration_minutes": 180,
            "server_url": "http://fruit.local:6655",
        }
        for key, value in settings.items():
            conn.execute(
                "INSERT INTO user_preferences(key,value) VALUES(?,?)",
                (f"setting:{key}", json.dumps(value)),
            )
        conn.commit()
        conn.close()
        self.env = patch.dict(os.environ, {
            "FRUIT_DB_PATH": str(self.db_path),
            "XTREAM_ENABLED": "true",
            "XTREAM_USERNAME": "demo user",
            "XTREAM_PASSWORD": "secret/pass",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client_patch = patch(
            "server.routes.api.xtream.XtreamClient", FakeXtreamClient
        )
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.client = create_app().test_client()

    def test_provider_to_browse_add_database_exports_and_tune(self):
        settings_page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("Persistent Channels", settings_page)
        self.assertIn("Browse Xtream Channels", settings_page)
        self.assertNotIn("XTREAM_USERNAME", settings_page)
        self.assertNotIn("XTREAM_PASSWORD", settings_page)

        categories = self.client.get("/api/xtream/categories")
        self.assertEqual(200, categories.status_code)
        self.assertEqual(["410"], [row["category_id"] for row in categories.get_json()["categories"]])

        browse = self.client.get(
            "/api/xtream/categories/410/streams",
            query_string={"q": "Washington", "page_size": 25},
        )
        self.assertEqual(200, browse.status_code)
        self.assertEqual("1904224", browse.get_json()["items"][0]["stream_id"])

        added = self.client.post("/api/xtream/persistent-channels", json={
            "category_id": "410",
            "stream_id": "1904224",
            "display_name": "Washington Nationals",
            "channel_number": "22",
            "favorite_team": "Washington Nationals",
        })
        self.assertEqual(201, added.status_code, added.get_data(as_text=True))
        channel = added.get_json()["channel"]

        conn = sqlite3.connect(self.db_path)
        stored = json.dumps(conn.execute(
            "SELECT * FROM xtream_persistent_channels WHERE id=?", (channel["id"],)
        ).fetchone())
        conn.close()
        self.assertNotIn("demo user", stored)
        self.assertNotIn("secret/pass", stored)

        m3u = self.client.get("/m3u/persistent").get_data(as_text=True)
        xml = self.client.get("/xmltv/persistent").get_data(as_text=True)
        self.assertIn("Washington Nationals", m3u)
        self.assertIn("Washington Nationals", xml)
        self.assertIn(f"/xtream/channel/{channel['id']}/stream", m3u)
        for output in (m3u, xml):
            self.assertNotIn("demo user", output)
            self.assertNotIn("secret", output)

        tuned = self.client.get(f"/xtream/channel/{channel['id']}/stream")
        self.assertEqual(302, tuned.status_code)
        self.assertEqual(
            "http://provider.example:8080/live/demo%20user/secret%2Fpass/1904224.ts",
            tuned.headers["Location"],
        )
        self.assertEqual("no-store", tuned.headers["Cache-Control"])
        self.assertEqual(302, self.client.head(f"/xtream/channel/{channel['id']}/stream").status_code)

        logs = "\n".join(line for _, line in get_recent_logs(count=200))
        self.assertNotIn("demo user", logs)
        self.assertNotIn("secret/pass", logs)

    def test_duplicate_validation_edit_disable_and_delete(self):
        payload = {
            "category_id": "410", "stream_id": "1904224",
            "display_name": "Washington Nationals", "channel_number": "22",
        }
        first = self.client.post("/api/xtream/persistent-channels", json=payload)
        channel_id = first.get_json()["channel"]["id"]
        duplicate = self.client.post(
            "/api/xtream/persistent-channels", json={**payload, "channel_number": "23"}
        )
        self.assertEqual(409, duplicate.status_code)
        conflict = self.client.post("/api/xtream/persistent-channels", json={
            "category_id": "410", "stream_id": "1904209",
            "display_name": "Miami Marlins", "channel_number": "22",
        })
        self.assertEqual(409, conflict.status_code)

        changed = self.client.patch(
            f"/api/xtream/persistent-channels/{channel_id}",
            json={"display_name": "Nationals Home", "enabled": False},
        )
        self.assertEqual("Nationals Home", changed.get_json()["channel"]["display_name"])
        self.assertNotIn("Nationals Home", self.client.get("/m3u/persistent").get_data(as_text=True))
        self.assertEqual(404, self.client.get(f"/xtream/channel/{channel_id}/stream").status_code)
        self.assertEqual(200, self.client.delete(f"/api/xtream/persistent-channels/{channel_id}").status_code)
        self.assertEqual([], self.client.get("/api/xtream/persistent-channels").get_json()["channels"])

    def test_unconfigured_category_is_not_browsed(self):
        response = self.client.get("/api/xtream/categories/999/streams")
        self.assertEqual(400, response.status_code)
        self.assertIn("configured", response.get_json()["message"])

    def test_arbitrary_provider_error_does_not_expose_credentials(self):
        class UnsafeClient(FakeXtreamClient):
            def get_live_categories(self):
                raise RuntimeError("http://provider/player_api.php?username=demo user&password=secret/pass")

        with patch("server.routes.api.xtream.XtreamClient", UnsafeClient):
            response = self.client.get("/api/xtream/categories")
        body = response.get_data(as_text=True)
        self.assertEqual(500, response.status_code)
        self.assertNotIn("demo user", body)
        self.assertNotIn("secret", body)
        logs = "\n".join(line for _, line in get_recent_logs(count=50))
        self.assertNotIn("secret/pass", logs)


if __name__ == "__main__":
    unittest.main()
