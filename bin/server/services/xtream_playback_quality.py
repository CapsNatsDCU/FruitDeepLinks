"""Measure playback's existing MPEG-TS bytes without opening another stream."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import threading
import time

from server.logging_setup import log
from server.services.xtream_persistent import save_stream_quality
from server.services.xtream_quality import _probe_bytes
from xtream_quality_sample import MAX_SAMPLE_BYTES, MAX_SAMPLE_SECONDS

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='playback-resolution')
_slots = threading.BoundedSemaphore(16)


def _save_sample(db_path, stream_id, category_id, sample):
    try:
        measured = _probe_bytes(sample)
    except Exception as exc:
        log(f'Playback resolution sample skipped: {type(exc).__name__}', 'DEBUG')
        return
    save_playback_quality(db_path, stream_id, category_id, measured)


def save_playback_quality(db_path, stream_id, category_id, measured):
    """Store valid playback measurements; failure never changes playback health."""
    try:
        # This is a new connection owned by the analysis thread, not Flask's
        # request connection or a playback lease. No provider clients are used.
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=rw', uri=True, timeout=10)
        try:
            categories = {str(category_id)} if category_id else set()
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('xtream_persistent_channels', 'xtream_channel_cache_streams'):
                if table in tables:
                    categories.update(row[0] for row in conn.execute(
                        f'SELECT category_id FROM {table} WHERE stream_id=?', (stream_id,)))
            if 'playables' in tables:
                columns = {row[1] for row in conn.execute('PRAGMA table_info(playables)')}
                if {'provider', 'stream_id', 'stream_metadata_json'} <= columns:
                    for row in conn.execute("SELECT stream_metadata_json FROM playables WHERE provider='xtream' AND stream_id=?", (stream_id,)):
                        try:
                            metadata = json.loads(row[0] or '{}')
                            if metadata.get('category_id'):
                                categories.add(str(metadata['category_id']))
                        except (ValueError, AttributeError):
                            continue
            for category in categories:
                save_stream_quality(conn, category, stream_id, measured)
            if categories:
                from server.services.xtream_background_quality import _ensure_state
                _ensure_state(conn)
                now = time.time()
                with conn:
                    conn.execute("INSERT INTO xtream_background_quality_channels(channel_id,last_attempt) "
                                 "SELECT id,? FROM xtream_persistent_channels WHERE stream_id=? "
                                 "ON CONFLICT(channel_id) DO UPDATE SET last_attempt=excluded.last_attempt", (now, stream_id))
                    if 'xtream_quality_queue' in tables:
                        conn.execute("UPDATE xtream_quality_queue SET state='completed',finished_at=?,last_error=NULL "
                                     "WHERE stream_id=? AND state IN ('pending','running') AND category_id IN "
                                     "(SELECT category_id FROM xtream_stream_quality WHERE stream_id=?)", (now, stream_id, stream_id))
                log('Playback video resolution updated from the existing stream', 'INFO')
        finally:
            conn.close()
    except Exception as exc:
        # A bad/short sample or SQLite failure must not disrupt playback or
        # overwrite a previous valid measurement. Never log URL-bearing text.
        log(f'Playback resolution sample skipped: {type(exc).__name__}', 'DEBUG')


class PlaybackQualityObserver:
    """Bounded tap: first eight seconds/4 MiB; local analysis runs off-thread."""
    def __init__(self, db_path, stream_id, category_id=None):
        self.db_path, self.stream_id, self.category_id = db_path, str(stream_id), category_id
        self.started = time.monotonic()
        self.sample = bytearray()
        self.done = not _slots.acquire(blocking=False)
        self.lock = threading.Lock()

    def feed(self, chunk):
        with self.lock:
            if self.done:
                return
            self.sample.extend(chunk[:MAX_SAMPLE_BYTES - len(self.sample)])
            if len(self.sample) >= MAX_SAMPLE_BYTES or time.monotonic() - self.started >= MAX_SAMPLE_SECONDS:
                self._finish()

    def close(self):
        with self.lock:
            if not self.done:
                self._finish()

    def _finish(self):
        self.done = True
        sample, self.sample = bytes(self.sample), bytearray()
        if not sample:
            _slots.release()
            return
        def analyse():
            try:
                _save_sample(self.db_path, self.stream_id, self.category_id, sample)
            finally:
                _slots.release()
        try:
            _executor.submit(analyse)
        except Exception:
            _slots.release()
