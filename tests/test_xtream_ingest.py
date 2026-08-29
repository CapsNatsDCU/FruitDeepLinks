import json
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from xtream_ingest import (  # noqa: E402
    XtreamClient,
    XtreamConfig,
    XtreamError,
    build_stream_url,
    ensure_schema,
    fetch_snapshot,
    ingest_payload,
    load_config,
    normalize_stream,
    parse_category_ids,
    parse_start_from_name,
    redact_credentials,
    stable_event_id,
)


def config(**overrides):
    values = {
        "enabled": True,
        "server_url": "http://provider.example:8080",
        "username": "user name",
        "password": "p@ss/word",
        "category_ids": ("10", "20"),
        "timezone_name": "America/New_York",
        "default_duration_minutes": 120,
    }
    values.update(overrides)
    return XtreamConfig(**values)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("request URL would normally appear here")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        return FakeResponse(self.payload)


class XtreamParsingTest(unittest.TestCase):
    def test_api_response_parsing_and_request_shape(self):
        session = FakeSession([{"category_id": "10", "category_name": "Sports"}, "bad"])
        client = XtreamClient(config(), session=session)
        result = client.get_live_categories()
        self.assertEqual(result, [{"category_id": "10", "category_name": "Sports"}])
        _, params, _ = session.calls[0]
        self.assertEqual(params["action"], "get_live_categories")
        self.assertEqual(params["username"], "user name")
        self.assertEqual(params["password"], "p@ss/word")

    def test_category_filtering_fetches_only_configured_ids(self):
        class Client:
            def __init__(self):
                self.calls = []

            def get_live_categories(self):
                return [
                    {"category_id": "10", "category_name": "One"},
                    {"category_id": "20", "category_name": "Two"},
                    {"category_id": "30", "category_name": "Do not fetch"},
                ]

            def get_live_streams(self, category_id):
                self.calls.append(category_id)
                return [{"stream_id": category_id}]

        client = Client()
        _, streams = fetch_snapshot(client, config())
        self.assertEqual(client.calls, ["10", "20"])
        self.assertEqual(set(streams), {"10", "20"})

    def test_category_ids_support_comma_and_json_but_never_empty_full_import(self):
        self.assertEqual(parse_category_ids("10, 20,10"), ("10", "20"))
        self.assertEqual(parse_category_ids('["30", 40]'), ("30", "40"))
        with self.assertRaises(XtreamError):
            config(category_ids=()).validate()

    def test_normalization_is_stable_and_marks_inferred_duration(self):
        stream = {
            "stream_id": 555,
            "name": "NHL | Capitals @ Lightning | 2026-09-01 7:00 PM",
            "stream_icon": "https://img.example/555.png",
            "epg_channel_id": "nhl.555",
            "container_extension": "m3u8",
        }
        first = normalize_stream(stream, "10", "NHL PPV", config())
        second = normalize_stream(stream, "10", "NHL PPV", config())
        self.assertEqual(first["event"]["id"], second["event"]["id"])
        self.assertEqual(first["event"]["id"], stable_event_id("10", 555))
        self.assertEqual(first["playable"]["provider"], "xtream")
        self.assertEqual(first["playable"]["logical_service"], "xtream")
        self.assertIsNone(first["playable"]["stream_url"])
        metadata = json.loads(first["playable"]["stream_metadata_json"])
        self.assertEqual(metadata["category_id"], "10")
        self.assertEqual(metadata["epg_channel_id"], "nhl.555")
        self.assertTrue(metadata["duration_inferred"])

    def test_time_only_name_is_not_assigned_a_date(self):
        self.assertIsNone(parse_start_from_name("NHL | Capitals @ Lightning | 7:00 PM"))
        self.assertIsNone(normalize_stream(
            {"stream_id": 1, "name": "NFL | Commanders at Ravens | 7:00 PM"},
            "10", "NFL", config(),
        ))

    def test_stream_url_encoding_and_redaction(self):
        cfg = config(category_ids=("10",))
        url = build_stream_url(cfg, "55/6", "ts")
        self.assertEqual(
            url,
            "http://provider.example:8080/live/user%20name/p%40ss%2Fword/55%2F6.ts",
        )
        diagnostic = f"raw=user name:p@ss/word url={url}"
        redacted = redact_credentials(diagnostic, cfg)
        self.assertNotIn("user name", redacted)
        self.assertNotIn("p@ss/word", redacted)
        self.assertNotIn("user%20name", redacted)
        self.assertNotIn("p%40ss%2Fword", redacted)

    def test_api_failure_message_never_contains_credentials(self):
        session = FakeSession([])
        session.get = lambda *args, **kwargs: FakeResponse([], status_code=500)
        client = XtreamClient(config(), session=session)
        with self.assertRaises(XtreamError) as caught:
            client.get_live_streams("10")
        message = str(caught.exception)
        self.assertNotIn("user name", message)
        self.assertNotIn("p@ss/word", message)

    def test_false_environment_value_is_a_hard_disable(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO user_preferences VALUES ('setting:xtream_enabled', 'true')"
        )
        loaded = load_config(conn, {
            "XTREAM_ENABLED": "false",
            "XTREAM_USERNAME": "user",
            "XTREAM_PASSWORD": "password",
        })
        self.assertFalse(loaded.enabled)


class XtreamStaleHandlingTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        ensure_schema(self.conn)
        self.cfg = config(category_ids=("10",), timezone_name="UTC")
        self.categories = [{"category_id": "10", "category_name": "Sports"}]

    def stream(self, stream_id):
        return {
            "stream_id": stream_id,
            "name": f"Event {stream_id}",
            "start_timestamp": 1_799_000_000,
            "end_timestamp": 1_799_007_200,
            "container_extension": "ts",
        }

    def test_stale_cleanup_is_xtream_scoped(self):
        ingest_payload(
            self.conn, self.categories, {"10": [self.stream(1), self.stream(2)]}, self.cfg,
            now=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.conn.execute(
            "INSERT INTO events (id, pvid, title, start_utc, end_utc, raw_attributes_json) "
            "VALUES ('other-event', 'other', 'Other', '2026-09-01T00:00:00Z', "
            "'2026-09-01T01:00:00Z', '{}')"
        )
        self.conn.execute(
            "INSERT INTO playables (event_id, playable_id, provider, logical_service) "
            "VALUES ('other-event', 'other-playable', 'peacock', 'peacock')"
        )
        self.conn.commit()

        result = ingest_payload(
            self.conn, self.categories, {"10": [self.stream(1)]}, self.cfg,
            now=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result["stale_removed"], 1)
        xtream_count = self.conn.execute(
            "SELECT COUNT(*) FROM playables WHERE provider='xtream'"
        ).fetchone()[0]
        other_count = self.conn.execute(
            "SELECT COUNT(*) FROM playables WHERE provider='peacock'"
        ).fetchone()[0]
        self.assertEqual(xtream_count, 1)
        self.assertEqual(other_count, 1)
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM events WHERE id='other-event'").fetchone()
        )


if __name__ == "__main__":
    unittest.main()
