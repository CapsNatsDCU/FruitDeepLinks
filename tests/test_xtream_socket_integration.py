"""Real loopback sockets/processes; opt in with RUN_XTREAM_SOCKET_TESTS=1.

No IPTV credentials or external provider traffic. FFmpeg is required for HLS.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from server.app import create_app
from server.services.xtream_persistent import create_channel
from server.services.xtream_quality import measure_stream_quality
from xtream_hls import HLSStream
from xtream_pool import XtreamPool
from tests.test_xtream_pool import account_rows, pool_environment


@unittest.skipUnless(os.getenv("RUN_XTREAM_SOCKET_TESTS") == "1", "opt-in loopback socket/FFmpeg integration")
class SocketIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'fruit.db'
        self.active = 0
        self.active_accounts = {}
        self.redirects = 0
        self.playback_fixture = None
        self.media_requests = 0
        self.api_requests = 0
        self.lock = threading.Lock()
        owner = self

        class Provider(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == '/player_api.php':
                    owner.api_requests += 1
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"user_info":{"auth":1,"status":"Active","max_connections":"1"}}')
                    return
                if path.endswith('/324969.ts') and path != '/media/324969.ts':
                    owner.redirects += 1
                    self.send_response(302)
                    self.send_header('Location', '/media/324969.ts?token=fixture')
                    self.end_headers()
                    return
                if path.endswith('101.ts'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
                    self.end_headers()
                    self.wfile.write(b'#EXTM3U\nhttp://this-url-must-never-reach-the-client\n')
                    return
                if path.endswith('.m3u8') or path.endswith('segment.ts'):
                    name = 'fixture.m3u8' if path.endswith('.m3u8') else 'segment.ts'
                    body = (owner.root / name).read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                account = path.split('/')[1]
                with owner.lock:
                    occupied = owner.active_accounts.get(account, 0) > 0
                    if not occupied:
                        owner.media_requests += 1
                        owner.active_accounts[account] = 1
                        owner.active += 1
                if occupied:
                    self.send_response(429)
                    self.end_headers()
                    return
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'video/mp2t')
                    self.end_headers()
                    while True:
                        self.wfile.write(owner.playback_fixture or b'\x47' * (188 * 64))
                        self.wfile.flush()
                        time.sleep(0.1 if owner.playback_fixture else 0.01)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with owner.lock:
                        owner.active -= 1
                        owner.active_accounts.pop(account, None)

        self.provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.provider.daemon_threads = True
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        self.addCleanup(self.provider.server_close)
        self.addCleanup(self.provider.shutdown)
        rows = account_rows((None, None, None))
        for row in rows:
            row['server_url'] = f'http://127.0.0.1:{self.provider.server_port}'
        self.env = {**pool_environment(rows), 'FRUIT_DB_PATH': str(self.path)}
        self.environment = patch.dict(os.environ, self.env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        with sqlite3.connect(self.path) as conn:
            for index, stream in enumerate(('100', '101', '324969')):
                create_channel(conn, {'stream_id': stream, 'name': 'Fixture'}, category_id='10', category_name='Fixtures', channel_number=str(index + 1))
        self.server = make_server('127.0.0.1', 0, create_app(), threaded=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        os.environ['SERVER_URL'] = self.base
        self.pool = XtreamPool(self.path)

    def until(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail('stream cleanup did not finish before deadline')

    def test_three_real_connections_disconnect_and_retry(self):
        streams = []
        playlist = requests.get(self.base + '/m3u/persistent', timeout=10)
        self.assertEqual(200, playlist.status_code)
        channel_paths = [line for line in playlist.text.splitlines() if '/xtream/channel/1/stream' in line]
        self.assertEqual(1, len(channel_paths), 'One channel entry must serve all three clients')
        try:
            for _ in range(3):
                response = requests.get(channel_paths[0], stream=True, timeout=10)
                streams.append(response)
                self.assertEqual(200, response.status_code)
                self.assertEqual(b'\x47' * 188, response.raw.read(188))
                self.assertNotIn('Location', response.headers)
            self.assertEqual(3, self.pool.status()['active'])
            self.assertEqual(3, self.active)
            self.assertEqual(3, len(self.active_accounts))
            self.assertEqual({1}, set(self.active_accounts.values()))
            self.assertTrue(all(a['last_media_success'] for a in self.pool.status()['accounts']))
            fourth = requests.get(self.base + '/xtream/channel/1/stream', timeout=10)
            self.assertEqual(503, fourth.status_code)
            streams.pop().close()
            self.until(lambda: self.pool.status()['active'] == 2 and self.active == 2)
            replacement = requests.get(self.base + '/xtream/channel/1/stream', stream=True, timeout=10)
            streams.append(replacement)
            self.assertEqual(200, replacement.status_code)
        finally:
            for response in streams:
                response.close()
        self.until(lambda: self.pool.status()['active'] == 0 and self.active == 0)

    def test_root_path_redirect_is_followed_and_hidden_from_client(self):
        response = requests.get(self.base + '/xtream/channel/3/stream', stream=True, timeout=10)
        try:
            self.assertEqual(200, response.status_code)
            self.assertEqual(b'\x47' * 188, response.raw.read(188))
            self.assertEqual(1, self.redirects)
            self.assertNotIn('Location', response.headers)
            self.assertEqual(1, self.pool.status()['active'])
        finally:
            response.close()
        self.until(lambda: self.pool.status()['active'] == 0 and self.active == 0)

    def test_resolution_worker_closes_media_before_local_ffprobe(self):
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                        '-i', 'color=c=blue:s=160x90:r=10', '-t', '2', '-c:v', 'mpeg2video',
                        '-f', 'mpegts', str(self.root / 'segment.ts')], check=True, capture_output=True)
        self.pool.check_accounts()
        def analyze(*args, **kwargs):
            self.assertEqual(0, self.pool.status()['active'])
            self.assertNotIn('http', ' '.join(args[0]))
            return subprocess.run(*args, **kwargs)
        measured = measure_stream_quality('segment', pool=self.pool, runner=analyze)
        self.assertEqual((160, 90), (measured['width'], measured['height']))
        self.assertEqual('mpeg2video', measured['codec'])
        self.assertEqual(0, self.pool.status()['active'])

    def test_manual_buttons_open_real_video_on_every_click(self):
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                        '-i', 'color=c=blue:s=160x90:r=10', '-t', '2', '-c:v', 'mpeg2video',
                        '-f', 'mpegts', str(self.root / 'segment.ts')], check=True, capture_output=True)
        self.playback_fixture = (self.root / 'segment.ts').read_bytes()
        for attempt in range(2):
            response = requests.post(self.base + '/api/xtream/pool/accounts/account_0/check', timeout=30)
            self.assertEqual(200, response.status_code)
            data = response.json()
            check = data['playback_checks']['account_0']
            self.assertEqual('passed', check['status'])
            self.assertEqual((160, 90), (check['video']['width'], check['video']['height']))
            self.assertEqual('100', check['channel']['stream_id'])
            self.assertEqual(attempt + 1, self.media_requests)
            self.assertEqual(0, self.api_requests)
            self.assertEqual(0, data['active'])
            self.until(lambda: self.active == 0)
        response = requests.post(self.base + '/api/xtream/pool/check', timeout=60)
        self.assertEqual(200, response.status_code)
        self.assertEqual(3, len(response.json()['playback_checks']))
        self.assertTrue(all(c['status'] == 'passed' for c in response.json()['playback_checks'].values()))
        self.assertEqual(5, self.media_requests)
        self.until(lambda: self.pool.status()['active'] == 0 and self.active == 0)

    def test_real_hls_remux_copies_playable_media_and_releases_process(self):
        self.assertIsNotNone(shutil.which('ffmpeg'), 'Docker/runtime needs FFmpeg for HLS remux')
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=10',
                        '-t', '2', '-c:v', 'mpeg2video', '-f', 'mpegts', str(self.root / 'segment.ts')], check=True, capture_output=True)
        (self.root / 'fixture.m3u8').write_text('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:2.0,\nsegment.ts\n#EXT-X-ENDLIST\n')
        response = requests.get(self.base + '/xtream/channel/2/stream', timeout=20)
        self.assertEqual(200, response.status_code, response.text[:100] if response.status_code != 200 else '')
        self.assertEqual('video/mp2t', response.headers['Content-Type'])
        self.assertGreater(len(response.content), 188)
        self.assertEqual(0x47, response.content[0])
        self.assertNotIn(b'#EXTM3U', response.content)
        self.until(lambda: self.pool.status()['active'] == 0)

    def test_hls_child_retains_capacity_after_parent_lease_descriptor_closes(self):
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=10',
                        '-t', '12', '-c:v', 'mpeg2video', '-f', 'mpegts', str(self.root / 'segment.ts')], check=True, capture_output=True)
        # An open live playlist keeps FFmpeg alive waiting for future segments.
        # Supply enough media for FFmpeg's normal stream probing to finish.
        (self.root / 'fixture.m3u8').write_text('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:12\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:12.0,\nsegment.ts\n')
        self.pool.check_accounts()
        lease = self.pool.acquire('101', 'hls-worker-crash')
        remux = None
        try:
            remux = HLSStream(f'http://127.0.0.1:{self.provider.server_port}/fixture.m3u8', 10, lease.fd)
            self.assertTrue(next(remux.chunks()).startswith(b'\x47'))
            # Closing the parent's descriptor simulates what SIGKILL does;
            # the child must hold the reservation until it too has stopped.
            os.close(lease.fd)
            lease.fd = -1
            self.assertIsNone(remux.process.poll())
            self.assertEqual(1, self.pool.status()['active'])
        finally:
            if remux is not None:
                remux.close()
            lease.release()
        self.until(lambda: self.pool.status()['active'] == 0)


if __name__ == '__main__':
    unittest.main()
