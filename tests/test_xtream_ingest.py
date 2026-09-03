import json
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
    parse_stop_from_name,
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


class FakeCompleted:
    def __init__(self, returncode=0, stdout="[]", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(self, completed=None, error=None):
        self.completed = completed or FakeCompleted()
        self.error = error
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.error:
            raise self.error
        return self.completed


class XtreamParsingTest(unittest.TestCase):
    def test_api_response_parsing_and_request_shape(self):
        session = FakeSession([{"category_id": "10", "category_name": "Sports"}, "bad"])
        runner = FakeRunner(error=AssertionError("curl must not run on requests success"))
        client = XtreamClient(config(), session=session, subprocess_runner=runner)
        result = client.get_live_categories()
        self.assertEqual(result, [{"category_id": "10", "category_name": "Sports"}])
        _, params, _ = session.calls[0]
        self.assertEqual(params["action"], "get_live_categories")
        self.assertEqual(params["username"], "user name")
        self.assertEqual(params["password"], "p@ss/word")
        self.assertEqual(runner.calls, [])

    def test_requests_failure_falls_back_to_curl(self):
        session = FakeSession([])
        session.get = lambda *args, **kwargs: FakeResponse([], status_code=503)
        runner = FakeRunner(FakeCompleted(stdout=json.dumps([
            {"stream_id": 55, "name": "NHL | 8/29 7pm Teams"}
        ])))
        client = XtreamClient(config(), session=session, subprocess_runner=runner)
        rows = client.get_live_streams("10")
        self.assertEqual(rows[0]["stream_id"], 55)
        command, kwargs = runner.calls[0]
        self.assertEqual(command[:5], ["curl", "-4", "-sS", "-L", "--max-time"])
        self.assertIn("category_id=10", command)
        self.assertNotIn("shell", kwargs)

    def test_unusable_requests_response_falls_back_to_curl(self):
        session = FakeSession({"user_info": {"auth": 1}})
        runner = FakeRunner(FakeCompleted(stdout='[{"category_id":"10"}]'))
        client = XtreamClient(config(), session=session, subprocess_runner=runner)
        self.assertEqual(client.get_live_categories(), [{"category_id": "10"}])
        self.assertEqual(len(runner.calls), 1)

    def test_empty_requests_response_is_confirmed_by_curl(self):
        session = FakeSession([])
        runner = FakeRunner(FakeCompleted(stdout='[{"category_id":"10"}]'))
        client = XtreamClient(config(), session=session, subprocess_runner=runner)
        self.assertEqual(client.get_live_categories(), [{"category_id": "10"}])
        self.assertEqual(len(runner.calls), 1)

    def test_invalid_curl_json_is_safe(self):
        session = FakeSession({"not": "a list"})
        runner = FakeRunner(FakeCompleted(stdout="not-json user name p@ss/word"))
        client = XtreamClient(config(), session=session, subprocess_runner=runner)
        with self.assertRaises(XtreamError) as caught:
            client.get_live_categories()
        message = str(caught.exception)
        self.assertIn("invalid JSON", message)
        self.assertNotIn("user name", message)
        self.assertNotIn("p@ss/word", message)

    def test_curl_process_error_is_safe(self):
        session = FakeSession([])
        runner = FakeRunner(error=RuntimeError(
            "curl failed for username=user name password=p@ss/word"
        ))
        client = XtreamClient(config(), session=session, subprocess_runner=runner)
        with self.assertRaises(XtreamError) as caught:
            client.get_live_categories()
        message = str(caught.exception)
        self.assertIn("could not run", message)
        self.assertNotIn("user name", message)
        self.assertNotIn("p@ss/word", message)

    def test_curl_failure_is_safe(self):
        session = FakeSession([])
        session.get = lambda *args, **kwargs: FakeResponse([], status_code=500)
        runner = FakeRunner(FakeCompleted(
            returncode=28,
            stderr="failed http://provider/player_api.php?username=user%20name&password=p%40ss%2Fword",
        ))
        client = XtreamClient(config(), session=session, subprocess_runner=runner)
        with self.assertRaises(XtreamError) as caught:
            client.get_live_streams("10")
        message = str(caught.exception)
        self.assertIn("exit code 28", message)
        for secret in ("user name", "p@ss/word", "user%20name", "p%40ss%2Fword"):
            self.assertNotIn(secret, message)

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

    def test_embedded_stop_time_is_provider_timing(self):
        stream = {
            "stream_id": 556,
            "name": "MLB 02 | Braves x Nationals start:2026-09-02 18:05:00 stop:2026-09-03 01:18:20",
        }
        now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
        normalized = normalize_stream(stream, "10", "MLB PPV", config(), now=now)
        self.assertEqual("2026-09-02T22:05:00Z", normalized["event"]["start_utc"])
        self.assertEqual("2026-09-03T05:18:20Z", normalized["event"]["end_utc"])
        metadata = json.loads(normalized["playable"]["stream_metadata_json"])
        self.assertFalse(metadata["duration_inferred"])
        self.assertEqual("provider", metadata["timing_source"])
        self.assertEqual(datetime(2026, 9, 3, 5, 18, 20, tzinfo=timezone.utc), parse_stop_from_name(stream["name"], "America/New_York"))

    def test_real_provider_name_without_year_or_colon(self):
        local_zone = ZoneInfo("America/New_York")
        now = datetime(2026, 8, 28, 17, 30, tzinfo=local_zone)
        parsed = parse_start_from_name(
            "NFL | 05 - 8/28 6pm Commanders at Ravens",
            "America/New_York",
            now=now,
            event_window_days=7,
        )
        self.assertEqual(parsed, datetime(2026, 8, 28, 22, 0, tzinfo=timezone.utc))

    def test_supported_yearless_provider_formats(self):
        now = datetime(2026, 8, 29, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        cases = {
            "NHL | 8/29 7pm Capitals at Rangers": (2026, 8, 29, 23, 0),
            "NHL | 08/29 7:00 PM Capitals at Rangers": (2026, 8, 29, 23, 0),
            "ESPN+ | 8/30 3pm Team A vs Team B": (2026, 8, 30, 19, 0),
            "ESPN+ | 8/30 15:30 Team A vs Team B": (2026, 8, 30, 19, 30),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    parse_start_from_name(
                        name, "America/New_York", now=now, event_window_days=7
                    ),
                    datetime(*expected, tzinfo=timezone.utc),
                )

    def test_yearless_dates_choose_previous_or_next_year_at_boundary(self):
        previous = parse_start_from_name(
            "NHL | 12/31 11pm Capitals at Rangers",
            "America/New_York",
            now=datetime(2027, 1, 1, 0, 30, tzinfo=ZoneInfo("America/New_York")),
            event_window_days=7,
        )
        following = parse_start_from_name(
            "NHL | 1/1 1am Capitals at Rangers",
            "America/New_York",
            now=datetime(2026, 12, 31, 23, 30, tzinfo=ZoneInfo("America/New_York")),
            event_window_days=7,
        )
        self.assertEqual(previous, datetime(2027, 1, 1, 4, 0, tzinfo=timezone.utc))
        self.assertEqual(following, datetime(2027, 1, 1, 6, 0, tzinfo=timezone.utc))

    def test_yearless_date_outside_import_window_is_rejected(self):
        self.assertIsNone(parse_start_from_name(
            "NHL | 8/29 7pm Capitals at Rangers",
            "America/New_York",
            now=datetime(2027, 1, 1, tzinfo=ZoneInfo("America/New_York")),
            event_window_days=7,
        ))

    def test_explicit_dates_outside_import_window_are_rejected(self):
        now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
        self.assertIsNone(parse_start_from_name(
            "NBA | 2025-01-18 00:20 Knicks x Timberwolves", "America/New_York", now=now, event_window_days=7
        ))
        self.assertIsNone(normalize_stream(
            {"stream_id": 2098, "name": "WNBA | 2098-12-31 18:00 Placeholder"},
            "10", "WNBA PPV", config(), now=now,
        ))

    def test_floracing_textual_date_uses_configured_timezone_and_year_inference(self):
        now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
        parsed = parse_start_from_name(
            "Short Track Super Series at Afton @ Sep 3 6:00 PM :Flo Racing 03",
            "America/New_York", now=now, event_window_days=7,
        )
        self.assertEqual(datetime(2026, 9, 3, 22, 0, tzinfo=timezone.utc), parsed)

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

    def test_saved_ui_value_overrides_false_environment_default(self):
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
        self.assertTrue(loaded.enabled)


class XtreamStaleHandlingTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        ensure_schema(self.conn)
        self.cfg = config(category_ids=("10",), timezone_name="UTC")
        self.categories = [{"category_id": "10", "category_name": "Sports"}]

    def stream(self, stream_id):
        start = int(datetime(2026, 8, 30, 18, tzinfo=timezone.utc).timestamp())
        return {
            "stream_id": stream_id,
            "name": f"Event {stream_id}",
            "start_timestamp": start,
            "end_timestamp": start + 7200,
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

    def test_observed_but_unparseable_stream_expires_prior_normalized_row(self):
        ingest_payload(
            self.conn, self.categories, {"10": [self.stream(7)]}, self.cfg,
            now=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        result = ingest_payload(
            self.conn,
            self.categories,
            {"10": [{
                "stream_id": 7,
                "name": "NHL | Capitals at Rangers | 7pm",
                "container_extension": "ts",
            }]},
            self.cfg,
            now=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        self.assertEqual(result["observed_upstream"], 1)
        self.assertEqual(result["normalized"], 0)
        self.assertEqual(result["stale_removed"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM playables WHERE provider='xtream'").fetchone()[0],
            0,
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM events WHERE id=?", (stable_event_id("10", 7),)
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
