import os
import unittest
from unittest.mock import patch

from tests import test_xtream_background_quality as fixtures
from tests.test_xtream_pool import account_rows, pool_environment
from server.services import xtream_quality_queue as queue
from server.services.xtream_background_quality import _next_channel, run_background_quality
from server.services.xtream_persistent import PersistentChannelError, quality_for_stream, create_channel, save_stream_quality
from server.services.xtream_quality import quality_probe_guard
from server.services.xtream_channel_cache import replace_snapshot
from xtream_ingest import XtreamError


class QualityQueueTests(unittest.TestCase):
    setUp = fixtures.BackgroundQualityTests.setUp

    def enqueue(self, stream='437220'):
        with self.pool.connection() as conn:
            return queue.enqueue(conn, 'sports', stream)

    def entries(self):
        with self.pool.connection() as conn:
            return queue.entries(conn)

    def measure(self, error=None):
        with patch('server.services.xtream_background_quality.XtreamClient') as client_type, \
             patch('server.services.xtream_background_quality._sample_media', return_value=b'fixture', side_effect=error) as sample, \
             patch('server.services.xtream_background_quality._probe_bytes', return_value={
                 'width':1920,'height':1080,'fps':59.94,'codec':'h264'}):
            client_type.return_value.get_probe_active_connections.return_value = 0
            outcome = run_background_quality(self.path, pool=self.pool)
            return outcome, sample

    def test_queue_is_durable_deduplicated_and_prioritized(self):
        first = self.enqueue()
        self.assertEqual(first['id'], self.enqueue()['id'])
        self.assertEqual(1, len(self.entries()))
        with self.pool.connection() as conn:
            self.assertEqual('437220', _next_channel(conn, 100000)['stream_id'])

    def test_new_additions_follow_manual_requests_then_leave_first_check_priority(self):
        manual = self.enqueue()
        with self.pool.connection() as conn:
            first = create_channel(conn, {'stream_id':'437221','name':'First new station'}, category_id='sports', category_name='Sports', channel_number='9')
            newest = create_channel(conn, {'stream_id':'437222','name':'Newest station'}, category_id='sports', category_name='Sports', channel_number='10')
            conn.execute("UPDATE xtream_persistent_channels SET created_at='2025-01-01T00:00:00Z' WHERE id IN (?,?)", (first['id'], newest['id']))
            save_stream_quality(conn, 'sports', '437222', {'width':1920,'height':1080})
            self.assertEqual(manual['id'], _next_channel(conn, 100000)['queue_id'])
            queue.cancel(conn, manual['id'])
        # Read on a new connection: priority survives a restarted worker.
        with self.pool.connection() as conn:
            self.assertEqual(newest['id'], _next_channel(conn, 100000)['id'])
            conn.execute('INSERT INTO xtream_background_quality_channels VALUES (?,?)', (newest['id'], 98000))
            self.assertEqual(first['id'], _next_channel(conn, 100000)['id'])
            conn.execute('INSERT INTO xtream_background_quality_channels VALUES (?,?)', (first['id'], 98000))
            self.assertEqual(7, _next_channel(conn, 100000)['id'])

    def test_queued_measurement_completes_and_saves_actual_quality(self):
        self.enqueue()
        outcome, sample = self.measure()
        self.assertEqual('measured', outcome)
        self.assertEqual('437220', sample.call_args.args[1])
        self.assertEqual('completed', self.entries()[0]['state'])
        with self.pool.connection() as conn:
            self.assertEqual(1080, quality_for_stream(conn, 'sports', '437220')['height'])
        self.assertEqual(0, self.pool.status()['active'])

    def test_queued_checks_reuse_the_account_after_ten_seconds(self):
        self.enqueue('437220')
        self.enqueue('437219')
        with patch('server.services.xtream_background_quality.time.time', return_value=100):
            self.assertEqual('measured', self.measure()[0])
        with patch('server.services.xtream_background_quality.time.time', return_value=109):
            self.assertEqual('account_interval', self.measure()[0])
        with patch('server.services.xtream_background_quality.time.time', return_value=110):
            self.assertEqual('measured', self.measure()[0])
        self.assertTrue(all(r['state'] == 'completed' for r in self.entries()))

    def test_visible_automatic_queue_follows_the_worker_order_without_provider_requests(self):
        from server.app import create_app
        self.enqueue('437220')
        with patch.dict(os.environ, {**pool_environment(account_rows((1,))), 'FRUIT_DB_PATH':str(self.path)}), \
             patch('server.services.xtream_background_quality.XtreamClient', side_effect=AssertionError('Queue reads are offline')):
            result = create_app().test_client().get('/api/xtream/persistent-channels/quality/queue').get_json()
        self.assertEqual('437220', result['requests'][0]['stream_id'])
        self.assertEqual(['437219'], [c['stream_id'] for c in result['automatic']])

    def test_cached_search_result_can_be_queued_without_saving_a_channel(self):
        with self.pool.connection() as conn:
            replace_snapshot(conn, [('sports','Sports')], [('sports','437221','Search result','search result',None,None,'ts')])
        self.enqueue('437221')
        outcome, sample = self.measure()
        self.assertEqual('measured', outcome)
        self.assertEqual('437221', sample.call_args.args[1])
        self.assertEqual('completed', self.entries()[0]['state'])

    def test_cancellation_preserves_previous_measurements_and_skips_request(self):
        request = self.enqueue()
        with self.pool.connection() as conn:
            queue.cancel(conn, request['id'])
            self.assertEqual('437219', _next_channel(conn, 100000)['stream_id'])
        self.assertEqual('cancelled', self.entries()[0]['state'])

    def test_stream_activity_keeps_request_waiting_without_provider_calls(self):
        self.enqueue()
        lease = self.pool.acquire('playing', 'persistent:1')
        try:
            with patch('server.services.xtream_background_quality.XtreamClient') as client_type:
                self.assertEqual('local_activity', run_background_quality(self.path, pool=self.pool))
                client_type.assert_not_called()
            self.assertEqual('pending', self.entries()[0]['state'])
        finally:
            lease.release()

    def test_existing_cooldown_is_enforced_for_queued_requests(self):
        self.enqueue()
        lease = self.pool.acquire('playing', 'persistent:1')
        lease.release()
        with patch('server.services.xtream_background_quality.quality_probe_guard', quality_probe_guard), \
             patch('server.services.xtream_background_quality.XtreamClient') as client_type:
            self.assertEqual('deferred', run_background_quality(self.path, pool=self.pool))
            client_type.assert_not_called()
        self.assertEqual('pending', self.entries()[0]['state'])

    def test_provider_occupied_request_waits_for_later_without_opening_media(self):
        self.enqueue()
        with patch('server.services.xtream_background_quality.XtreamClient') as client_type, \
             patch('server.services.xtream_background_quality._sample_media') as sample:
            client_type.return_value.get_probe_active_connections.return_value = 1
            self.assertEqual('provider_occupied_or_unknown', run_background_quality(self.path, pool=self.pool))
            sample.assert_not_called()
        self.assertEqual('pending', self.entries()[0]['state'])

    def test_failure_is_terminal_and_credential_safe(self):
        self.enqueue()
        outcome, _ = self.measure(XtreamError('secret URL must never be returned'))
        self.assertEqual('error', outcome)
        self.assertEqual('failed', self.entries()[0]['state'])
        self.assertNotIn('secret', self.entries()[0]['last_error'])
        self.enqueue()
        self.assertEqual('pending', self.entries()[0]['state'])
        self.assertIsNone(self.entries()[0]['last_error'])

    def test_invalid_video_sample_marks_failure_without_a_saved_measurement(self):
        self.enqueue()
        with patch('server.services.xtream_background_quality.XtreamClient') as client_type, \
             patch('server.services.xtream_background_quality._sample_media', return_value=b'invalid'), \
             patch('server.services.xtream_background_quality._probe_bytes', side_effect=XtreamError('no video')):
            client_type.return_value.get_probe_active_connections.return_value = 0
            self.assertEqual('error', run_background_quality(self.path, pool=self.pool))
        self.assertEqual('failed', self.entries()[0]['state'])
        with self.pool.connection() as conn:
            self.assertIsNone(quality_for_stream(conn, 'sports', '437220'))

    def test_interrupted_worker_request_recovers_and_running_cannot_be_cancelled(self):
        request = self.enqueue()
        with self.pool.connection() as conn:
            queue.mark(conn, request['id'], 'running')
            with self.assertRaises(PersistentChannelError):
                queue.cancel(conn, request['id'])
        self.assertEqual('measured', self.measure()[0])
        self.assertEqual('completed', self.entries()[0]['state'])

    def test_arbitrary_stream_ids_are_rejected_and_enqueue_does_not_call_provider(self):
        from server.app import create_app
        environment = {**pool_environment(account_rows((1,))), 'FRUIT_DB_PATH':str(self.path)}
        with patch.dict(os.environ, environment), patch('server.services.xtream_background_quality.XtreamClient') as provider:
            api = create_app().test_client()
            good = api.post('/api/xtream/persistent-channels/quality/queue', json={'category_id':'sports','stream_id':'437220'})
            self.assertEqual(202, good.status_code)
            bad = api.post('/api/xtream/persistent-channels/quality/queue', json={'category_id':'sports','stream_id':'arbitrary'})
            self.assertEqual(400, bad.status_code)
            self.assertEqual(200, api.get('/api/xtream/persistent-channels/quality/queue').status_code)
            provider.assert_not_called()
