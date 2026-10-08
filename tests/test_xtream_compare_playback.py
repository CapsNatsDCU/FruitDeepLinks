import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from tests import test_xtream_endpoint_contract as endpoint_fixture
from tests.test_xtream_pool import pool_environment
from xtream_compare_playback import METHODS, compare, run_probe
from xtream_pool import XtreamPool
from server.services.xtream_persistent import create_channel


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.provider = endpoint_fixture.EndpointContractTests()
        self.provider.setUp()
        self.addCleanup(self.provider.doCleanups)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = self.provider.config
        self.rows = [{'id': 'account_2', 'server_url': config.server_url,
                      'username': config.username, 'password': config.password,
                      'fallback_server_url': config.server_url.replace('127.0.0.1', 'localhost')}]
        self.env = pool_environment(self.rows)
        self.pool = XtreamPool(self.root / 'fruit.db', self.env)
        with self.pool.connection() as conn:
            create_channel(conn, {'stream_id': '100', 'name': 'Fixture'},
                           category_id='10', category_name='Fixture', channel_number='1')

    @unittest.skipUnless(shutil.which('curl') and shutil.which('ffmpeg') and shutil.which('ffprobe'), 'media tools required')
    def test_real_three_method_comparison_validates_video_and_cleans_gate(self):
        path = self.root / 'segment.ts'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                        '-i', 'color=c=blue:s=160x90:r=10', '-t', '2', '-c:v', 'mpeg2video',
                        '-f', 'mpegts', str(path)], check=True, capture_output=True)
        self.provider.ts_sample = path.read_bytes()
        result = compare(self.pool, 'account_2', '100')
        self.assertEqual(list(METHODS), [row['method'] for row in result['results']])
        self.assertTrue(all(row['status'] == 'passed' for row in result['results']), result)
        self.assertTrue(all((row['video']['width'], row['video']['height']) == (160, 90) for row in result['results']))
        self.assertEqual(200, result['results'][0]['http_status'])
        self.assertEqual(1, result['results'][0]['redirects'])
        self.assertFalse(self.pool.status()['accounts'][0]['busy'])
        self.assertEqual(0, self.pool.status()['active'])
        self.assertNotIn(self.provider.username, json.dumps(result))
        self.assertNotIn(self.provider.password, json.dumps(result))
        self.assertNotIn('http://', json.dumps(result))
        self.assertTrue(all(params.get('action') is None for _, params, _ in self.provider.requests))

    def test_busy_account_sends_no_provider_request(self):
        with self.pool.gate.hold(self.pool.accounts[0].config):
            result = compare(self.pool, 'account_2', '100')
        self.assertEqual('account_in_use', result['reason'])
        self.assertEqual([], self.provider.requests)

    def test_disabled_account_sends_no_provider_request(self):
        self.pool.update('account_2', {'enabled': False})
        self.assertEqual('account_disabled', compare(self.pool, 'account_2', '100')['reason'])
        self.assertEqual([], self.provider.requests)

    def test_alternate_host_is_pinned_and_gate_is_held_across_every_method(self):
        calls = []
        def probe(method, target, fd, seconds):
            self.assertTrue(self.pool.status()['accounts'][0]['busy'])
            calls.append((method, target[0].server_url, target[0].username, target[1], fd))
            return {'method': method, 'status': 'failed'}
        with patch('xtream_compare_playback.run_probe', side_effect=probe):
            compare(self.pool, 'account_2', '100', host='alternate')
        self.assertEqual(list(METHODS), [call[0] for call in calls])
        self.assertTrue(all(call[1] == self.rows[0]['fallback_server_url'] for call in calls))
        self.assertTrue(all(call[2] == self.rows[0]['username'] and call[3] == '100' for call in calls))
        self.assertEqual(1, len({call[4] for call in calls}))
        self.assertFalse(self.pool.status()['accounts'][0]['busy'])

    def test_downloaded_script_lists_ids_using_container_pythonpath_without_network(self):
        copied = self.root / 'downloaded-script.py'
        copied.write_bytes((Path(__file__).resolve().parents[1] / 'bin/xtream_compare_playback.py').read_bytes())
        env = {**os.environ, **self.env, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'bin')}
        result = subprocess.run([sys.executable, str(copied), '--db', str(self.pool.db_path), '--list'],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stdout)
        data = json.loads(result.stdout)
        self.assertEqual('account_2', data['accounts'][0]['id'])
        self.assertEqual('100', data['first_50_saved_channels'][0]['stream_id'])
        self.assertNotIn(self.provider.username, result.stdout)
        self.assertNotIn(self.provider.password, result.stdout)
        self.assertEqual([], self.provider.requests)

    def test_live_endpoint_prefix_is_only_used_when_explicitly_selected(self):
        with patch('xtream_compare_playback.run_probe', return_value={'status': 'failed'}) as probe:
            compare(self.pool, 'account_2', '100', path_style='live')
        self.assertEqual(3, probe.call_count)
        self.assertTrue(all(call.args[1][0].server_url == self.rows[0]['server_url'] + '/live'
                            for call in probe.call_args_list))
        self.assertFalse(self.pool.status()['accounts'][0]['busy'])

    def test_hard_deadline_stops_slow_trickle_and_releases_gate(self):
        class SlowProvider(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(b'\x47')
                        self.wfile.flush()
                        time.sleep(0.2)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), SlowProvider)
        server.daemon_threads = True
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.rows[0]['server_url'] = f'http://127.0.0.1:{server.server_port}'
        pool = XtreamPool(self.pool.db_path, pool_environment(self.rows))
        started = time.monotonic()
        with pool.gate.hold(pool.accounts[0].config) as fd:
            result = run_probe('python_ts', (pool.accounts[0].config, '100'), fd, 5)
        self.assertLess(time.monotonic() - started, 7)
        self.assertEqual('network_deadline', result['error'])
        self.assertFalse(pool.status()['accounts'][0]['busy'])
