import json
import os
import sqlite3
import sys
import time
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
    quality_for_stream,
    reconcile_channels,
    render_m3u,
    render_xmltv,
    save_stream_quality,
    update_channel,
)
from tests.xtream_test_helpers import HealthyAccountClient, mocked_provider
from xtream_ingest import (  # noqa: E402
    XtreamConfig,
    XtreamError,
    ensure_schema as ensure_ingest_schema,
    ingest_payload,
    normalize_stream,
)
from xtream_pool import PoolUnavailable, XtreamPool  # noqa: E402


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

    def test_quality_cache_is_keyed_by_category_and_stream(self):
        measured = save_stream_quality(self.conn, "410", "1904224", {
            "width": 1920, "height": 1080, "fps": 59.94, "codec": "h264",
        })
        self.assertEqual(1920, measured["width"])
        self.assertIsNone(quality_for_stream(self.conn, "999", "1904224"))
        channel = self.add()
        self.assertEqual(1080, channel["measured_quality"]["height"])
        self.assertIsNone(channel["advertised_quality"])
        self.assertEqual("4K", page_streams([{"stream_id": 1, "name": "FOX 5 4K RAW"}])["items"][0]["advertised_quality"])
        self.assertEqual("4K", page_streams([{"stream_id": 2, "name": "FOX 5 ⁴ᴷ RAW"}])["items"][0]["advertised_quality"])

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

    def test_absent_category_snapshot_is_not_authoritative_channel_removal(self):
        channel = self.add()
        reconcile_channels(self.conn, {})
        self.assertEqual('available', get_channel(self.conn, channel['id'])['availability_status'])
        reconcile_channels(self.conn, {'410': []})
        self.assertEqual('unavailable', get_channel(self.conn, channel['id'])['availability_status'])


class FakeXtreamClient:
    def __init__(self, *args, **kwargs):
        pass

    def get_live_categories(self):
        return [FIXTURE["category"], {"category_id": "999", "category_name": "Not configured"}]

    def get_live_streams(self, category_id):
        if str(category_id) not in {"410", "999"}:
            raise AssertionError("Only a provider category may be fetched")
        return list(FIXTURE["streams"])

    def get_all_live_streams(self):
        return [
            {**stream, "category_id": category_id}
            for category_id in ("410", "999")
            for stream in FIXTURE["streams"]
        ]


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
            "XTREAM_SERVER_URL": "http://provider.example:8080",
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
        self.pool = XtreamPool(self.db_path, client_factory=HealthyAccountClient)
        self.pool.check_accounts()

    def test_provider_to_browse_add_database_exports_and_tune(self):
        settings_page = self.client.get("/settings").get_data(as_text=True)
        persistent_page = self.client.get("/persistent-channels").get_data(as_text=True)
        self.assertIn("Persistent Channels", settings_page)
        self.assertNotIn("Search Xtream Channels", settings_page)
        self.assertIn('href="/persistent-channels"', settings_page)
        self.assertIn("Search Xtream Channels", persistent_page)
        self.assertIn("All Categories", persistent_page)
        self.assertIn("All Active Categories", persistent_page)
        self.assertNotIn('id="persistent-category"', persistent_page)
        self.assertIn('editor.dataset.categoryId = stream.category_id', persistent_page)
        self.assertIn("Refresh Persistent Guide", persistent_page)
        self.assertNotIn("Refresh Persistent Guide", settings_page)
        self.assertIn('href="/persistent-channels" class="topnav-link topnav-link-active"', persistent_page)
        for page in (settings_page, persistent_page):
            self.assertNotIn("XTREAM_USERNAME", page)
            self.assertNotIn("XTREAM_PASSWORD", page)

        categories = self.client.get("/api/xtream/categories")
        self.assertEqual(200, categories.status_code)
        # Reads use the persisted selection/catalog cache and never browse the
        # provider.  A selected category that has not been explicitly scanned
        # remains visible rather than silently disappearing.
        self.assertEqual(["410"], [row["category_id"] for row in categories.get_json()["categories"]])
        self.assertTrue(next(row for row in categories.get_json()["categories"] if row["category_id"] == "410")["selected"])
        self.assertNotIn("demo user", categories.get_data(as_text=True))
        self.assertNotIn("secret/pass", categories.get_data(as_text=True))

        saved = self.client.post("/api/xtream/categories", json={"category_ids": []})
        self.assertEqual(200, saved.status_code)
        self.assertEqual([], saved.get_json()["selected_category_ids"])
        conn = sqlite3.connect(self.db_path)
        stored_selection = conn.execute(
            "SELECT value FROM user_preferences WHERE key='setting:xtream_category_ids'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual('""', stored_selection)

        restored = self.client.post("/api/xtream/categories", json={"category_ids": ["410"]})
        self.assertEqual(200, restored.status_code)

        browse = self.client.post(
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

        with mocked_provider() as upstream:
            tuned = self.client.get(f"/xtream/channel/{channel['id']}/stream")
            self.assertEqual(200, tuned.status_code)
            self.assertNotIn("Location", tuned.headers)
            self.assertEqual("http://provider.example:8080/demo%20user/secret%2Fpass/1904224.ts", upstream.get.call_args.args[0])
            tuned.close()
        self.assertEqual("no-store", tuned.headers["Cache-Control"])
        self.assertEqual(200, self.client.head(f"/xtream/channel/{channel['id']}/stream").status_code)

        logs = "\n".join(line for _, line in get_recent_logs(count=200))
        self.assertNotIn("demo user", logs)
        self.assertNotIn("secret/pass", logs)

    def test_catalog_scan_reports_database_contention(self):
        with patch("server.routes.api.xtream.ensure_sports_schema",
                   side_effect=sqlite3.OperationalError("database is locked")):
            response = self.client.post("/api/xtream/discovery/scan")
        self.assertEqual(503, response.status_code)
        self.assertIn("busy", response.get_json()["message"])

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

    def test_unselected_provider_category_can_be_browsed_and_added(self):
        live = self.client.post("/api/xtream/categories/live")
        self.assertEqual(200, live.status_code)
        categories = {row["category_id"]: row for row in live.get_json()["categories"]}
        self.assertEqual({"410", "999"}, set(categories))
        self.assertTrue(categories["410"]["selected"])
        self.assertFalse(categories["999"]["selected"])

        browse = self.client.post("/api/xtream/categories/999/streams", query_string={"q": "Washington"})
        self.assertEqual(200, browse.status_code)
        self.assertEqual("1904224", browse.get_json()["items"][0]["stream_id"])
        added = self.client.post("/api/xtream/persistent-channels", json={
            "category_id": "999", "stream_id": "1904224",
            "display_name": "Washington Nationals", "channel_number": "22",
        })
        self.assertEqual(201, added.status_code, added.get_data(as_text=True))
        self.assertEqual("999", added.get_json()["channel"]["category_id"])
        self.assertEqual(["410"], self.client.get("/api/xtream/categories").get_json()["selected_category_ids"])

    def test_channel_search_spans_all_or_only_active_categories(self):
        all_results = self.client.post(
            "/api/xtream/persistent-channels/search",
            query_string={"q": "Washington", "scope": "all", "page_size": 1},
        )
        self.assertEqual(200, all_results.status_code)
        self.assertEqual(2, all_results.get_json()["total"])
        first = all_results.get_json()["items"][0]
        self.assertEqual("410", first["category_id"])
        self.assertEqual("MLB TEAM PPV", first["category_name"])
        second = self.client.post(
            "/api/xtream/persistent-channels/search",
            query_string={"q": "Washington", "scope": "all", "page": 2, "page_size": 1},
        )
        self.assertEqual("999", second.get_json()["items"][0]["category_id"])
        active = self.client.post(
            "/api/xtream/persistent-channels/search",
            query_string={"q": "Washington", "scope": "active"},
        )
        self.assertEqual(200, active.status_code)
        self.assertEqual(1, active.get_json()["total"])
        self.assertEqual("410", active.get_json()["items"][0]["category_id"])
        self.assertEqual(["410"], self.client.get("/api/xtream/categories").get_json()["selected_category_ids"])

    def test_search_falls_back_when_full_stream_list_lacks_categories(self):
        class CategorylessClient(FakeXtreamClient):
            def get_all_live_streams(self):
                return list(FIXTURE["streams"])

        with patch("server.routes.api.xtream.XtreamClient", CategorylessClient):
            response = self.client.post(
                "/api/xtream/persistent-channels/search",
                query_string={"q": "Washington", "scope": "all"},
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual({"410", "999"}, {row["category_id"] for row in response.get_json()["items"]})

    def test_search_falls_back_when_full_stream_request_is_unsupported(self):
        class CategoryOnlyClient(FakeXtreamClient):
            def get_all_live_streams(self):
                raise XtreamError("Provider does not support full live stream lists")

        with patch("server.routes.api.xtream.XtreamClient", CategoryOnlyClient):
            response = self.client.post(
                "/api/xtream/persistent-channels/search",
                query_string={"q": "Washington", "scope": "active"},
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.get_json()["total"])
        self.assertEqual("410", response.get_json()["items"][0]["category_id"])

    def test_search_requires_name_and_valid_scope(self):
        for params in ({"scope": "all"}, {"q": "Washington", "scope": "other"}):
            response = self.client.post("/api/xtream/persistent-channels/search", query_string=params)
            self.assertEqual(400, response.status_code)

    def test_quality_endpoint_validates_provider_identity_and_persists_result(self):
        with patch("server.services.xtream_quality.measure_stream_quality", return_value={
            "width": 1280, "height": 720, "fps": 60.0, "codec": "h264",
        }) as probe:
            result = self.client.post("/api/xtream/persistent-channels/quality", json={
                "category_id": "410", "stream_id": "1904224",
            })
        self.assertEqual(200, result.status_code, result.get_data(as_text=True))
        self.assertEqual(720, result.get_json()["measured_quality"]["height"])
        probe.assert_called_once_with("1904224", "ts", guarded=False)
        search = self.client.post("/api/xtream/persistent-channels/search", query_string={"q": "Washington"})
        self.assertEqual(720, search.get_json()["items"][0]["measured_quality"]["height"])
        with patch("server.services.xtream_quality.time.time", return_value=time.time() + 31):
            invalid = self.client.post("/api/xtream/persistent-channels/quality", json={
                "category_id": "410", "stream_id": "unknown",
            })
        self.assertEqual(400, invalid.status_code)

    def test_configured_channel_quality_uses_saved_identity_without_catalog_requests(self):
        created = self.client.post("/api/xtream/persistent-channels", json={
            "category_id": "410", "stream_id": "1904224",
            "display_name": "Washington Nationals", "channel_number": "22",
        })
        self.assertEqual(201, created.status_code)
        with (patch("server.routes.api.xtream._configured_client",
                    side_effect=AssertionError("Catalog must not be queried")),
              patch("server.services.xtream_quality.measure_stream_quality", return_value={
                  "width": 1280, "height": 720, "fps": 60.0, "codec": "h264",
              }) as probe):
            result = self.client.post("/api/xtream/persistent-channels/quality", json={
                "category_id": "410", "stream_id": "1904224",
            })
        self.assertEqual(200, result.status_code, result.get_data(as_text=True))
        probe.assert_called_once_with("1904224", "ts", guarded=False)
        self.assertEqual(720, result.get_json()["measured_quality"]["height"])

    def test_probe_pacing_rejects_before_catalog_or_media_requests(self):
        from server.services.xtream_quality import quality_probe_guard
        with quality_probe_guard(self.db_path):
            pass
        with (patch("server.routes.api.xtream._configured_client") as catalog,
              patch("server.services.xtream_quality.measure_stream_quality") as media):
            result = self.client.post("/api/xtream/persistent-channels/quality", json={
                "category_id": "410", "stream_id": "1904224",
            })
        self.assertEqual(429, result.status_code)
        self.assertEqual("quality_probe_deferred", result.get_json()["code"])
        self.assertEqual("30", result.headers["Retry-After"])
        catalog.assert_not_called()
        media.assert_not_called()

    def test_degraded_pool_rejects_quality_before_catalog_media_or_verification(self):
        with self.pool.connection() as conn:
            conn.execute("UPDATE xtream_account_state SET health='degraded'")
        before = self.pool.status()
        with (patch("server.routes.api.xtream._configured_client") as catalog,
              patch("server.services.xtream_quality.measure_stream_quality") as media,
              patch("xtream_pool.XtreamPool.check_accounts") as verify):
            result = self.client.post("/api/xtream/persistent-channels/quality", json={
                "category_id": "410", "stream_id": "1904224",
            })
        self.assertEqual(429, result.status_code)
        self.assertIn("verified healthy", result.get_json()["message"])
        self.assertEqual("120", result.headers["Retry-After"])
        catalog.assert_not_called()
        media.assert_not_called()
        verify.assert_not_called()
        self.assertEqual(before, self.pool.status())

    def test_busy_healthy_pool_rejects_before_catalog_or_media(self):
        lease = self.pool.acquire("playing", "persistent:1")
        try:
            with (patch("server.routes.api.xtream._configured_client") as catalog,
                  patch("server.services.xtream_quality.measure_stream_quality") as media):
                result = self.client.post("/api/xtream/persistent-channels/quality", json={
                    "category_id": "410", "stream_id": "1904224",
                })
            self.assertEqual(503, result.status_code)
            catalog.assert_not_called()
            media.assert_not_called()
        finally:
            lease.release()

    def test_quality_catalog_fallback_contains_only_healthy_available_accounts(self):
        from tests.test_xtream_pool import pool_environment
        with patch.dict(os.environ, pool_environment()):
            pool = XtreamPool(self.db_path, client_factory=HealthyAccountClient)
            pool.check_accounts()
            with pool.connection() as conn:
                conn.execute("UPDATE xtream_account_state SET health='degraded' WHERE account_id='account_0'")
            lease = pool.acquire("playing", "persistent:1", excluded=("account_0",))
            try:
                catalog_client = FakeXtreamClient()
                with (patch("server.routes.api.xtream.XtreamClient", return_value=catalog_client),
                      patch("server.services.xtream_quality.measure_stream_quality", return_value={
                          "width": 1280, "height": 720, "fps": 60.0, "codec": "h264",
                      })):
                    result = self.client.post("/api/xtream/persistent-channels/quality", json={
                        "category_id": "410", "stream_id": "1904224",
                    })
                self.assertEqual(200, result.status_code, result.get_data(as_text=True))
                self.assertEqual((pool.accounts[2].config,), catalog_client.metadata_configs)
            finally:
                lease.release()

    def test_quality_endpoint_reports_busy_pool_without_a_retry_storm(self):
        with patch("server.services.xtream_quality.measure_stream_quality",
                   side_effect=PoolUnavailable("All Xtream playback slots are occupied or unavailable")):
            result = self.client.post("/api/xtream/persistent-channels/quality", json={
                "category_id": "410", "stream_id": "1904224",
            })
        self.assertEqual(503, result.status_code)
        self.assertEqual("capacity_unavailable", result.get_json()["code"])

    def test_unknown_provider_category_is_not_browsed_or_added(self):
        response = self.client.post("/api/xtream/categories/888/streams")
        self.assertEqual(400, response.status_code)
        self.assertIn("not currently available", response.get_json()["message"])
        added = self.client.post("/api/xtream/persistent-channels", json={
            "category_id": "888", "stream_id": "1904224",
            "display_name": "Washington Nationals", "channel_number": "22",
        })
        self.assertEqual(400, added.status_code)

    def test_cached_category_get_does_not_contact_or_expose_provider(self):
        class UnsafeClient(FakeXtreamClient):
            def get_live_categories(self):
                raise RuntimeError("http://provider/player_api.php?username=demo user&password=secret/pass")

        with patch("server.routes.api.xtream.XtreamClient", UnsafeClient):
            response = self.client.get("/api/xtream/categories")
        body = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertNotIn("demo user", body)
        self.assertNotIn("secret", body)
        logs = "\n".join(line for _, line in get_recent_logs(count=50))
        self.assertNotIn("secret/pass", logs)

    def test_tune_get_never_creates_an_empty_database(self):
        self.db_path.unlink()
        response = self.client.get("/xtream/channel/1/stream")
        self.assertEqual(404, response.status_code)
        self.assertFalse(self.db_path.exists())


if __name__ == "__main__":
    unittest.main()
