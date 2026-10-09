import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from server.services.xtream_persistent import create_channel, quality_for_stream
from server.services.xtream_playback_test import test_account_playback
from server.services.xtream_quality import _eligible_probe_configs, QualityProbeDeferred
from xtream_pool import XtreamPool
from tests.test_xtream_pool import pool_environment


class ManualPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.factory = Mock()
        self.pool = XtreamPool(Path(self.temp.name) / 'fruit.db', pool_environment(),
                               client_factory=self.factory)
        with self.pool.connection() as conn:
            create_channel(conn, {'stream_id': '100', 'name': 'Fixture'},
                           category_id='10', category_name='Fixtures', channel_number='1')
        self.sample = patch('server.services.xtream_playback_test._sample_media', return_value=b'video').start()
        self.probe = patch('server.services.xtream_playback_test._probe_bytes',
                           return_value={'width': 160, 'height': 90, 'codec': 'mpeg2video', 'fps': 10}).start()
        self.addCleanup(patch.stopall)

    def test_every_click_samples_selected_account_despite_cached_health_and_cooldown(self):
        for health in ('unknown', 'unreachable', 'unhealthy', 'degraded', 'healthy'):
            with self.subTest(health=health):
                with self.pool.connection() as conn:
                    conn.execute("UPDATE xtream_account_state SET health=?,retry_after=? WHERE account_id='account_1'",
                                 (health, time.time() + 300))
                for _ in range(2):
                    result = test_account_playback(self.pool, 'account_1')
                    self.assertEqual('passed', result['playback_checks']['account_1']['status'])
                    lease = self.sample.call_args.args[0]
                    self.assertEqual('account_1', lease.account.id)
                    self.assertEqual('manual_playback_test', lease.source)
                    self.assertEqual(False, self.sample.call_args.kwargs['require_quiet'])
                    self.assertEqual(0, result['active'])
                    account = result['accounts'][1]
                    self.assertTrue(account['last_media_success'])
                    self.assertIsNone(account['last_success'])
                    self.assertEqual(0, account['retry_after_seconds'])
                    self.assertEqual('healthy' if health == 'healthy' else 'degraded', account['health'])
        self.assertEqual(10, self.sample.call_count)
        self.factory.assert_not_called()

    def test_success_does_not_enable_automatic_quality_probe(self):
        test_account_playback(self.pool, 'account_0')
        with self.assertRaises(QualityProbeDeferred):
            _eligible_probe_configs(self.pool)

    def test_manual_playback_updates_channel_quality_without_an_extra_sample(self):
        test_account_playback(self.pool, 'account_0')
        self.probe.return_value = {'width':1280, 'height':720, 'fps':59.94, 'codec':'h264'}
        test_account_playback(self.pool, 'account_0')
        with self.pool.connection() as conn:
            self.assertEqual(720, quality_for_stream(conn, '10', '100')['height'])
        self.assertEqual(2, self.sample.call_count)
        self.factory.assert_not_called()

    def test_later_metadata_outage_preserves_playback_retry_after_media_success(self):
        test_account_playback(self.pool, 'account_0')
        self.factory.return_value.get_account_max_connections.side_effect = OSError('API unavailable')
        result = self.pool.check_accounts('account_0')
        account = result['accounts'][0]
        self.assertEqual('degraded', account['health'])
        self.assertEqual('ready_degraded', account['availability_reason'])
        self.assertIsNone(account['last_success'])

    def test_failure_stays_on_selected_account_and_hides_credentials(self):
        self.sample.side_effect = RuntimeError('private-user-0 private-password/0')
        result = test_account_playback(self.pool, 'account_0')
        self.assertEqual('failed', result['playback_checks']['account_0']['status'])
        self.assertEqual(1, self.sample.call_count)
        self.assertEqual(0, result['active'])
        self.assertNotIn('private-', json.dumps(result))
        self.probe.assert_not_called()

    def test_invalid_video_cannot_pass(self):
        self.probe.side_effect = ValueError('not video')
        result = test_account_playback(self.pool, 'account_0')
        self.assertEqual('failed', result['playback_checks']['account_0']['status'])
        self.assertIsNone(result['accounts'][0]['last_media_success'])
        self.assertEqual(0, result['active'])

    def test_timeout_releases_account_and_next_click_retries(self):
        self.sample.side_effect = [TimeoutError(), b'video']
        first = test_account_playback(self.pool, 'account_0')
        self.assertEqual('Playback sample timed out', first['playback_checks']['account_0']['message'])
        self.assertEqual(0, first['active'])
        self.assertEqual('passed', test_account_playback(self.pool, 'account_0')['playback_checks']['account_0']['status'])

    def test_test_all_skips_disabled_and_occupied_without_interrupting_stream(self):
        self.pool.update('account_2', {'enabled': False})
        lease = self.pool.acquire('100', 'manual_playback_test', account_id='account_0')
        try:
            result = test_account_playback(self.pool)
            self.assertEqual({'account_0': 'occupied', 'account_2': 'disabled'}, result['checks_skipped'])
            self.assertEqual({'account_1'}, set(result['playback_checks']))
            self.assertEqual(1, result['active'])
            self.assertEqual('account_1', self.sample.call_args.args[0].account.id)
        finally:
            lease.release()
        self.assertEqual(0, self.pool.status()['active'])

    def test_provider_operation_skips_account_even_without_media_lease(self):
        with self.pool.gate.hold(self.pool.accounts[0].config):
            result = test_account_playback(self.pool, 'account_0')
        self.assertEqual({'account_0': 'occupied'}, result['checks_skipped'])
        self.sample.assert_not_called()

    def test_empty_catalog_discovers_channel_with_only_selected_credentials(self):
        client = self.factory.return_value
        client.get_live_categories.return_value = [{'category_id': '10'}]
        client.get_live_streams.return_value = [{'stream_id': '200', 'name': 'Discovered', 'container_extension': 'ts'}]
        with patch('server.services.xtream_playback_test._saved_channel', return_value=None):
            result = test_account_playback(self.pool, 'account_2')
        self.factory.assert_called_once_with(self.pool.accounts[2].config)
        client.get_live_streams.assert_called_once_with('10')
        client.session.close.assert_called_once()
        self.assertEqual('200', result['playback_checks']['account_2']['channel']['stream_id'])
        self.assertEqual('200', self.sample.call_args.args[0].stream_id)

    def test_all_accounts_receive_independent_fresh_samples(self):
        result = test_account_playback(self.pool)
        self.assertEqual(3, self.sample.call_count)
        self.assertEqual({'account_0', 'account_1', 'account_2'},
                         {call.args[0].account.id for call in self.sample.call_args_list})
        self.assertTrue(all(check['status'] == 'passed' for check in result['playback_checks'].values()))
        self.assertEqual(0, result['active'])
