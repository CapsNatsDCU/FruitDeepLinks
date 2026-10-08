"""Check actual outgoing request formatting against a strict loopback provider."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from xtream_curl import CurlStream
from xtream_epg import _provider_xmltv_one
from xtream_hls import HLSStream
from xtream_ingest import XtreamClient, XtreamConfig, build_stream_url
from server.services.xtream_quality import _probe_bytes


class EndpointContractTests(unittest.TestCase):
    def setUp(self):
        self.username = 'user +&=%/#?é'
        self.password = 'pass +&=%/#?é\\"'
        self.requests = []
        self.ts_sample = None
        owner = self

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                params = parse_qs(parsed.query)
                segments = [unquote(part) for part in parsed.path.split('/')]
                owner.requests.append((parsed.path, params, segments))
                if parsed.path in ('/portal/player_api.php', '/portal/xmltv.php'):
                    if params.get('username') != [owner.username] or params.get('password') != [owner.password]:
                        self.send_response(403)
                        self.end_headers()
                        return
                    if parsed.path.endswith('xmltv.php'):
                        body = b'<tv><channel id="fixture"><display-name>Fixture</display-name></channel></tv>'
                    else:
                        action = params.get('action', [None])[0]
                        if action is None:
                            payload = {'user_info': {'auth': 1, 'status': 'Active', 'max_connections': '1'}}
                        elif action == 'get_live_categories':
                            payload = [{'category_id': '10', 'category_name': 'Fixture'}]
                        elif action == 'get_live_streams':
                            payload = [{'stream_id': '100', 'name': 'Fixture'}]
                        elif action in ('get_short_epg', 'get_simple_data_table'):
                            payload = {'epg_listings': [{'title': 'Fixture', 'start_timestamp': '1791450000'}]}
                        else:
                            self.send_response(404)
                            self.end_headers()
                            return
                        body = json.dumps(payload).encode()
                elif len(segments) == 5 and segments[1] == 'portal' and segments[2:4] == [owner.username, owner.password]:
                    self.send_response(302)
                    self.send_header('Location', '/redirected-hls?token=fixture' if segments[-1].endswith('.m3u8') else '/redirected-media?token=fixture')
                    self.end_headers()
                    return
                elif parsed.path == '/redirected-media':
                    body = b'\x47' * 376
                elif parsed.path == '/redirected-hls':
                    body = b'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nsegment.ts\n#EXT-X-ENDLIST\n'
                elif parsed.path == '/segment.ts' and owner.ts_sample:
                    body = owner.ts_sample
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.config = XtreamConfig(True, f'http://127.0.0.1:{server.server_port}/portal',
                                   self.username, self.password, ('10',))
        self.client = XtreamClient(self.config, timeout=2)
        self.addCleanup(self.client.session.close)

    def test_requests_metadata_and_epg_send_exact_decoded_parameters(self):
        self.assertEqual(1, self.client.get_account_max_connections())
        self.client.get_live_categories()
        self.client.get_live_streams('category +&=%/#?é')
        self.client.get_all_live_streams()
        self.client.get_short_epg('stream +&=%/#?é')
        self.client.get_epg('stream +&=%/#?é')
        self.assertEqual(6, len(self.requests))
        self.assertTrue(all(path == '/portal/player_api.php' for path, _, _ in self.requests))
        self.assertNotIn('action', self.requests[0][1])
        self.assertEqual(['get_live_categories'], self.requests[1][1]['action'])
        self.assertEqual(['category +&=%/#?é'], self.requests[2][1]['category_id'])
        self.assertNotIn('category_id', self.requests[3][1])
        self.assertEqual(['get_short_epg'], self.requests[4][1]['action'])
        self.assertEqual(['get_simple_data_table'], self.requests[5][1]['action'])
        self.assertTrue(all(params['username'] == [self.username] and params['password'] == [self.password]
                            for _, params, _ in self.requests))

    @unittest.skipUnless(shutil.which('curl'), 'curl required')
    def test_curl_metadata_encodes_credentials_identically_to_requests(self):
        for action, category, stream in ((None, None, None), ('get_live_categories', None, None),
                                        ('get_live_streams', 'category +&=%/#?é', None),
                                        ('get_short_epg', None, 'stream +&=%/#?é'),
                                        ('get_simple_data_table', None, 'stream +&=%/#?é')):
            self.client._curl_payload(action, category, stream)
            path, params, _ = self.requests[-1]
            self.assertEqual('/portal/player_api.php', path)
            self.assertEqual([self.username], params['username'])
            self.assertEqual([self.password], params['password'])
            if action:
                self.assertEqual([action], params['action'])
            if category:
                self.assertEqual([category], params['category_id'])
            if stream:
                self.assertEqual([stream], params['stream_id'])

    def test_xmltv_uses_exact_credentials_and_separate_endpoint(self):
        channels = {}
        result = _provider_xmltv_one(self.client, {'fixture'}, self.config, channels)
        self.assertEqual({'fixture': []}, result)
        self.assertEqual(['Fixture'], channels['fixture']['names'])
        self.assertEqual('/portal/xmltv.php', self.requests[-1][0])
        self.assertEqual({'username': [self.username], 'password': [self.password]}, self.requests[-1][1])

    def test_media_path_has_encoded_segments_and_follows_redirect(self):
        stream_id = 'stream +&=%/#?é'
        with self.client.session.get(build_stream_url(self.config, stream_id), timeout=2) as response:
            self.assertEqual(200, response.status_code)
            self.assertEqual(b'\x47' * 376, response.content)
        self.assertEqual(['', 'portal', self.username, self.password, stream_id + '.ts'], self.requests[0][2])
        self.assertEqual({}, self.requests[0][1])
        self.assertEqual('/redirected-media', self.requests[1][0])

    @unittest.skipUnless(shutil.which('curl'), 'curl required')
    def test_curl_media_preserves_path_encoding_and_follows_redirect(self):
        read_fd, lease_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, lease_fd)
        stream = CurlStream(build_stream_url(self.config, '100'), 2, lease_fd)
        try:
            self.assertEqual(b'\x47' * 376, b''.join(stream.chunks()))
        finally:
            stream.close()
        self.assertEqual(['', 'portal', self.username, self.password, '100.ts'], self.requests[0][2])
        self.assertEqual('/redirected-media', self.requests[1][0])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_hls_preserves_encoded_credentials_across_redirect_and_relative_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'segment.ts'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                            '-i', 'color=c=blue:s=160x90:r=10', '-t', '2', '-c:v', 'mpeg2video',
                            '-f', 'mpegts', str(path)], check=True, capture_output=True)
            self.ts_sample = path.read_bytes()
        read_fd, lease_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, lease_fd)
        stream = HLSStream(build_stream_url(self.config, '100', 'm3u8'), 3, lease_fd)
        try:
            video = _probe_bytes(b''.join(stream.chunks()))
        finally:
            stream.close()
        self.assertEqual((160, 90), (video['width'], video['height']))
        self.assertEqual(['', 'portal', self.username, self.password, '100.m3u8'], self.requests[0][2])
        self.assertIn('/redirected-hls', [path for path, _, _ in self.requests])
        self.assertIn('/segment.ts', [path for path, _, _ in self.requests])
