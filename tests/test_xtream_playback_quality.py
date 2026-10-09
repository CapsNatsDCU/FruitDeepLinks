"""Playback measurements reuse delivered bytes and never own provider capacity."""
import os
import sqlite3
import unittest
from unittest.mock import Mock, patch
from contextlib import contextmanager

from tests import test_xtream_proxy as fixtures
from tests.xtream_test_helpers import FakeMedia, mocked_provider
from server.app import create_app
from server.services import xtream_playback_quality as playback
from server.services.xtream_proxy import proxy_stream
from server.services.xtream_persistent import create_channel, quality_for_stream, save_stream_quality
from server.services.xtream_channel_cache import replace_snapshot
from server.services import xtream_quality_queue as queue

MEASURED = {'width':1280, 'height':720, 'fps':59.94, 'codec':'h264'}


class PlaybackQualityTests(unittest.TestCase):
    setUp = fixtures.StreamProxyTests.setUp

    @contextmanager
    def local_analysis(self):
        jobs = []
        with patch.object(playback._executor, 'submit', side_effect=lambda job: jobs.append(job)), \
             patch.object(playback, '_probe_bytes', return_value=MEASURED) as probe:
            try:
                yield jobs, probe
            finally:
                for job in jobs:
                    job()
                jobs.clear()

    def save_channel(self):
        with self.pool.connection() as conn:
            return create_channel(conn, {'stream_id':'437219','name':'Sports'}, category_id='sports', category_name='Sports', channel_number='7')

    def test_playback_updates_quality_with_one_media_request_and_completes_matching_queue(self):
        channel = self.save_channel()
        with self.pool.connection() as conn:
            save_stream_quality(conn, 'sports', '437219', {'width':1920,'height':1080})
            queued = queue.enqueue(conn, 'sports', '437219')
        chunks = [b'\x47' * 376, b'\x47' * 564]
        with self.local_analysis() as (jobs, probe), \
             patch('server.services.xtream_proxy.PlaybackQualityObserver', playback.PlaybackQualityObserver), \
             mocked_provider(FakeMedia(chunks)) as session:
            response = create_app().test_client().get(f'/xtream/channel/{channel["id"]}/stream', buffered=False)
            self.assertEqual(200, response.status_code)
            self.assertEqual(1, self.pool.status()['active'])
            probe.assert_not_called()
            self.assertEqual(b''.join(chunks), response.get_data())
            response.close()
            self.assertEqual(0, self.pool.status()['active'])
            self.assertEqual(1, session.get.call_count)
            self.assertEqual(1, len(jobs))
            job = jobs.pop(); job()
            probe.assert_called_once_with(b''.join(chunks))
        with self.pool.connection() as conn:
            self.assertEqual(720, quality_for_stream(conn, 'sports', '437219')['height'])
            self.assertEqual('completed', queue.entries(conn)[0]['state'])
            self.assertEqual(queued['id'], queue.entries(conn)[0]['id'])
            self.assertIsNotNone(conn.execute('SELECT last_attempt FROM xtream_background_quality_channels WHERE channel_id=?', (channel['id'],)).fetchone())

    def test_every_start_submits_a_new_sample_and_head_does_not_measure(self):
        self.save_channel()
        with self.local_analysis() as (jobs, probe), \
             patch('server.services.xtream_proxy.PlaybackQualityObserver', playback.PlaybackQualityObserver), \
             mocked_provider() as session:
            head = self.client.head('/stream')
            head.close()
            session.get.assert_not_called()
            for _ in range(2):
                response = self.client.get('/stream', buffered=False)
                response.get_data(); response.close()
            self.assertEqual(2, len(jobs))
            self.assertEqual(2, session.get.call_count)
            probe.assert_not_called()

    def test_never_started_body_skips_analysis_and_close_is_idempotent(self):
        with self.local_analysis() as (jobs, probe), \
             patch('server.services.xtream_proxy.PlaybackQualityObserver', playback.PlaybackQualityObserver), \
             self.app.test_request_context('/stream'), mocked_provider():
            response = proxy_stream('437219', 'unstarted', pool=self.pool)
            response.close(); response.close()
            self.assertEqual([], jobs)
            self.assertEqual(0, self.pool.status()['active'])

    def test_capture_byte_cap_and_deadline_do_not_stop_media(self):
        with self.local_analysis() as (jobs, probe), patch.object(playback, 'MAX_SAMPLE_BYTES', 376):
            observer = playback.PlaybackQualityObserver(self.path, '437219', 'sports')
            observer.feed(b'\x47' * 564)
            observer.feed(b'tail not part of sample')
            observer.close()
            self.assertEqual(1, len(jobs))
            self.assertEqual(0, len(observer.sample))
            jobs.pop()()
            probe.assert_called_once_with(b'\x47' * 376)
        with self.local_analysis() as (jobs, probe), patch.object(playback.time, 'monotonic', side_effect=[100, 109]):
            observer = playback.PlaybackQualityObserver(self.path, '437219', 'sports')
            observer.feed(b'\x47' * 188)
            observer.close()
            self.assertEqual(1, len(jobs))

    def test_invalid_sample_keeps_previous_measurement_and_waiting_request(self):
        self.save_channel()
        with self.pool.connection() as conn:
            save_stream_quality(conn, 'sports', '437219', MEASURED)
            queue.enqueue(conn, 'sports', '437219')
            before = quality_for_stream(conn, 'sports', '437219')
        with patch.object(playback, '_probe_bytes', side_effect=ValueError('secret URL')):
            playback._save_sample(self.path, '437219', 'sports', b'invalid')
        with self.pool.connection() as conn:
            self.assertEqual(before, quality_for_stream(conn, 'sports', '437219'))
            self.assertEqual('pending', queue.entries(conn)[0]['state'])

    def test_lane_without_explicit_category_resolves_cached_and_ingested_identities(self):
        with self.pool.connection() as conn:
            replace_snapshot(conn, [('news','News')], [('news','437219','News','news',None,None,'ts')])
            conn.execute('CREATE TABLE playables(provider TEXT,stream_id TEXT,stream_metadata_json TEXT)')
            conn.execute('INSERT INTO playables VALUES (?,?,?)', ('xtream','437219','{"category_id":"events"}'))
        with patch.object(playback, '_probe_bytes', return_value=MEASURED):
            playback._save_sample(self.path, '437219', None, b'fixture')
        with self.pool.connection() as conn:
            self.assertEqual(720, quality_for_stream(conn, 'news', '437219')['height'])
            self.assertEqual(720, quality_for_stream(conn, 'events', '437219')['height'])

    def test_observer_failure_never_changes_delivered_media_or_lease_cleanup(self):
        broken = Mock()
        broken.feed.side_effect = OSError('private URL')
        broken.close.side_effect = OSError('private URL')
        chunks = [b'\x47' * 376, b'\x47' * 188]
        with patch('server.services.xtream_proxy.PlaybackQualityObserver', return_value=broken), mocked_provider(FakeMedia(chunks)):
            response = self.client.get('/stream', buffered=False)
            self.assertEqual(b''.join(chunks), response.get_data())
            response.close()
        self.assertEqual(0, self.pool.status()['active'])

    def test_bound_on_pending_analysis_and_submit_failure_releases_slot(self):
        semaphore = __import__('threading').BoundedSemaphore(1)
        with patch.object(playback, '_slots', semaphore), self.local_analysis() as (jobs, probe):
            first = playback.PlaybackQualityObserver(self.path, '437219', 'sports')
            second = playback.PlaybackQualityObserver(self.path, '437219', 'sports')
            second.feed(b'ignored'); second.close()
            self.assertTrue(second.done)
            first.feed(b'\x47'); first.close()
            jobs.pop()()
            self.assertTrue(semaphore.acquire(blocking=False))
            semaphore.release()
        with patch.object(playback, '_slots', semaphore), patch.object(playback._executor, 'submit', side_effect=RuntimeError()):
            observer = playback.PlaybackQualityObserver(self.path, '437219', 'sports')
            observer.feed(b'\x47'); observer.close()
            self.assertTrue(semaphore.acquire(blocking=False))
            semaphore.release()
