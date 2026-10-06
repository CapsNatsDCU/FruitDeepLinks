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
from xtream_transport import configure_session, proxy_url


class XtreamTransportTests(unittest.TestCase):
    def test_proxy_validation_never_echoes_invalid_value(self):
        for value in ("http://user:secret@proxy:8888", "https://proxy:8888",
                      "http://proxy:8888/path", "http://proxy:8888?token=secret",
                      "http://proxy:bad"):
            with self.subTest(value=value), patch.dict(os.environ, {"XTREAM_HTTP_PROXY": value}):
                with self.assertRaisesRegex(ValueError, "XTREAM_HTTP_PROXY must be") as error:
                    proxy_url()
                self.assertNotIn("secret", str(error.exception))

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
            self.assertEqual("http://127.0.0.1:8888", popen.call_args.kwargs["env"]["http_proxy"])
            self.assertNotIn("fixture-password", " ".join(ffmpeg_args))
            stream.close()

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
