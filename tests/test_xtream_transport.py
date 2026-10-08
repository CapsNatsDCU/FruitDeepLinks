"""The dedicated Xtream proxy covers metadata and media transports."""
import json
import os
import shutil
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from xtream_curl import CurlStream
from xtream_hls import HLSStream
from xtream_ingest import XtreamClient, XtreamConfig
from xtream_transport import configure_session, media_chunks, proxy_url, user_agent


class XtreamTransportTests(unittest.TestCase):
    def test_agent_validation_and_override(self):
        with patch.dict(os.environ, {"XTREAM_USER_AGENT": "FixturePlayer/1.0"}):
            self.assertEqual("FixturePlayer/1.0", user_agent())
        for value in ("", "private\r\nAuthorization: secret", "x" * 257, "private\x7fsecret"):
            with self.subTest(value=value), patch.dict(os.environ, {"XTREAM_USER_AGENT": value}):
                with self.assertRaisesRegex(ValueError, "XTREAM_USER_AGENT must be") as error:
                    user_agent()
                self.assertNotIn("secret", str(error.exception))

    def test_media_prefix_is_forwarded_before_large_buffer_fills_without_replay(self):
        release_rest = threading.Event()
        prefix = b"\x47" + b"a" * 187 + b"\x47" + b"b" * 187
        rest = b"\x47" + b"c" * 187
        body = prefix + rest

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(prefix)
                self.wfile.flush()
                if release_rest.wait(2):
                    self.wfile.write(rest)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # A reader that still waits for 12 KiB would time out while the
        # provider deliberately holds back the remaining media.
        with requests.get(f"http://127.0.0.1:{server.server_port}/stream",
                          stream=True, timeout=(1, 0.5)) as response:
            try:
                chunks = media_chunks(response)
                self.assertEqual(prefix, next(chunks))
                release_rest.set()
                self.assertEqual(body, prefix + b"".join(chunks))
            finally:
                release_rest.set()

    def test_proxy_validation_never_echoes_invalid_value(self):
        for value in ("http://user:secret@proxy:8888", "https://proxy:8888",
                      "http://proxy:8888/path", "http://proxy:8888?token=secret",
                      "http://proxy:bad"):
            with self.subTest(value=value), patch.dict(os.environ, {"XTREAM_HTTP_PROXY": value}):
                with self.assertRaisesRegex(ValueError, "XTREAM_HTTP_PROXY must be") as error:
                    proxy_url()
                self.assertNotIn("secret", str(error.exception))

    def test_chunked_response_continues_after_the_startup_prefix(self):
        prefix = b"\x47" + b"a" * 187 + b"\x47" + b"b" * 187
        rest = (b"\x47" + b"c" * 187) * 100
        body = prefix + rest

        class Provider(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for part in (prefix, rest[:5000], rest[5000:]):
                        self.wfile.write(f"{len(part):X}\r\n".encode() + part + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with requests.get(f"http://127.0.0.1:{server.server_port}/stream", stream=True,
                          timeout=(1, 1)) as response:
            self.assertTrue(response.raw.chunked)
            chunks = media_chunks(response)
            self.assertEqual(prefix, next(chunks))
            self.assertEqual(body, prefix + b"".join(chunks))

    def test_session_is_scoped_to_xtream(self):
        ordinary = requests.Session()
        self.addCleanup(ordinary.close)
        with patch.dict(os.environ, {"XTREAM_HTTP_PROXY": "http://127.0.0.1:8888"}):
            xtream = configure_session(requests.Session())
        self.addCleanup(xtream.close)
        self.assertEqual({"http": "http://127.0.0.1:8888",
                          "https": "http://127.0.0.1:8888"}, xtream.proxies)
        self.assertFalse(xtream.trust_env)
        self.assertEqual({}, ordinary.proxies)
        self.assertEqual("KSPlayer", xtream.headers["User-Agent"])
        self.assertTrue(ordinary.headers["User-Agent"].startswith("python-requests"))

    def test_requests_and_curl_account_checks_use_proxy(self):
        if not shutil.which("curl"):
            self.skipTest("curl is unavailable")
        received = []

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                received.append(self.path)
                body = json.dumps({"user_info": {"auth": 1, "status": "Active",
                                                 "max_connections": "1"}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        config = XtreamConfig(True, "http://provider.invalid", "fixture-user",
                              "fixture-password", ("10",))
        with patch.dict(os.environ, {"XTREAM_HTTP_PROXY":
                                          f"http://127.0.0.1:{server.server_port}"}):
            client = XtreamClient(config, timeout=3)
            try:
                self.assertEqual(1, client.get_account_max_connections())
                self.assertEqual("healthy", client.last_account_check["health"])
                self.assertEqual("1", client._curl_payload(None)["user_info"]["max_connections"])
            finally:
                client.session.close()
        self.assertEqual(2, len(received))
        self.assertTrue(all(path.startswith("http://provider.invalid/player_api.php?")
                            for path in received))

    def test_media_children_receive_proxy_without_authenticated_url_in_arguments(self):
        process = Mock()
        process.stdin.closed = False
        process.stdout.closed = False
        process.poll.return_value = 0
        url = "http://provider.invalid/fixture-user/fixture-password/7.ts"
        with patch.dict(os.environ, {"XTREAM_HTTP_PROXY": "http://127.0.0.1:8888"}):
            with patch("xtream_curl.subprocess.Popen", return_value=process) as popen:
                stream = CurlStream(url, 5, 42)
            curl_args = popen.call_args.args[0]
            self.assertEqual("KSPlayer", curl_args[curl_args.index("--user-agent") + 1])
            self.assertEqual("http://127.0.0.1:8888", curl_args[curl_args.index("--proxy") + 1])
            self.assertNotIn("fixture-password", " ".join(curl_args))
            stream.close()

            process.reset_mock()
            process.stdin.closed = False
            process.stdout.closed = False
            process.poll.return_value = 0
            with patch("xtream_hls.subprocess.Popen", return_value=process) as popen:
                stream = HLSStream(url, 5, 42)
            ffmpeg_args = popen.call_args.args[0]
            self.assertEqual("KSPlayer", ffmpeg_args[ffmpeg_args.index("-user_agent") + 1])
            self.assertEqual("http://127.0.0.1:8888", popen.call_args.kwargs["env"]["http_proxy"])
            self.assertNotIn("fixture-password", " ".join(ffmpeg_args))
            port = stream.bootstrap.server_port
            seed_url = ffmpeg_args[ffmpeg_args.index("-i") + 1]
            self.assertEqual(404, requests.get(f"http://127.0.0.1:{port}/unknown", timeout=2).status_code)
            self.assertIn(url, requests.get(seed_url, timeout=2).text)
            stream.close()
            self.assertFalse(stream.bootstrap_thread.is_alive())
            with self.assertRaises(requests.ConnectionError):
                requests.get(seed_url, timeout=0.5)

    def test_hls_nested_request_reaches_proxy(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg is unavailable")
        received = []

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                received.append(self.path)
                self.send_response(404)
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        read_fd, lease_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, lease_fd)
        with patch.dict(os.environ, {"XTREAM_HTTP_PROXY":
                                          f"http://127.0.0.1:{server.server_port}"}):
            stream = HLSStream("http://provider.invalid/fixture-user/fixture-password/7.m3u8",
                               5, lease_fd)
            try:
                with self.assertRaises(OSError):
                    list(stream.chunks())
            finally:
                stream.close()
        self.assertEqual(1, len(received))


if __name__ == "__main__":
    unittest.main()
