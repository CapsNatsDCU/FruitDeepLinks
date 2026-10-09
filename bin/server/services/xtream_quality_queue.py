"""Durable operator requests for cached channel resolution checks."""
import sqlite3
import time

from server.services.xtream_persistent import PersistentChannelError, ensure_schema as ensure_channels


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS xtream_quality_queue (
        id INTEGER PRIMARY KEY, category_id TEXT NOT NULL, stream_id TEXT NOT NULL,
        channel_name TEXT NOT NULL, stream_extension TEXT NOT NULL,
        state TEXT NOT NULL, requested_at REAL NOT NULL, started_at REAL,
        finished_at REAL, last_error TEXT,
        UNIQUE(category_id,stream_id))""")


def entries(conn):
    try:
        rows = conn.execute("SELECT * FROM xtream_quality_queue ORDER BY "
                            "CASE state WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, "
                            "CASE WHEN state IN ('pending','running') THEN requested_at ELSE -requested_at END LIMIT 250")
        columns = [item[0] for item in rows.description]
        return [dict(zip(columns, row)) for row in rows]
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc).lower():
            raise
        return []


def known_channel(conn, category_id, stream_id):
    from server.services.xtream_channel_cache import get_stream
    ensure_channels(conn)
    row = conn.execute("SELECT id,display_name,stream_extension,availability_status FROM xtream_persistent_channels "
                       "WHERE category_id=? AND stream_id=?", (category_id, stream_id)).fetchone()
    if row:
        if row[3] != 'available':
            raise PersistentChannelError('The saved channel is unavailable or needs attention')
        return {'id': row[0], 'name': row[1], 'extension': row[2]}
    stream = get_stream(conn, category_id, stream_id)
    if not stream:
        raise PersistentChannelError('Select a saved channel or a channel from the saved search results')
    return {'id': None, 'name': stream['name'], 'extension': stream.get('container_extension') or 'ts'}


def enqueue(conn, category_id, stream_id):
    category_id, stream_id = str(category_id or '').strip(), str(stream_id or '').strip()
    if not category_id or not stream_id or len(category_id) > 255 or len(stream_id) > 255:
        raise PersistentChannelError('Category and stream IDs are required')
    channel = known_channel(conn, category_id, stream_id)
    ensure_schema(conn)
    with conn:
        existing = conn.execute("SELECT id,state FROM xtream_quality_queue WHERE category_id=? AND stream_id=?",
                                (category_id, stream_id)).fetchone()
        if existing and existing[1] in {'pending', 'running'}:
            return next(item for item in entries(conn) if item['id'] == existing[0])
        if conn.execute("SELECT COUNT(*) FROM xtream_quality_queue WHERE state IN ('pending','running')").fetchone()[0] >= 200:
            raise PersistentChannelError('The queue is full (200 channels); wait for checks to finish or cancel requests')
        conn.execute("""INSERT INTO xtream_quality_queue
            (category_id,stream_id,channel_name,stream_extension,state,requested_at)
            VALUES (?,?,?,?,'pending',?) ON CONFLICT(category_id,stream_id) DO UPDATE SET
            channel_name=excluded.channel_name,stream_extension=excluded.stream_extension,state='pending',
            requested_at=excluded.requested_at,started_at=NULL,finished_at=NULL,last_error=NULL""",
            (category_id, stream_id, channel['name'], channel['extension'], time.time()))
        # Bound retained history without deleting active work.
        conn.execute("DELETE FROM xtream_quality_queue WHERE state NOT IN ('pending','running') AND id NOT IN "
                     "(SELECT id FROM xtream_quality_queue WHERE state NOT IN ('pending','running') ORDER BY requested_at DESC LIMIT 50)")
    return next(item for item in entries(conn) if item['category_id'] == category_id and item['stream_id'] == stream_id)


def cancel(conn, queue_id):
    ensure_schema(conn)
    with conn:
        row = conn.execute('SELECT state FROM xtream_quality_queue WHERE id=?', (queue_id,)).fetchone()
        if not row:
            raise PersistentChannelError('Queue request not found')
        if row[0] == 'running':
            raise PersistentChannelError('This check is already running; its current short sample must finish')
        conn.execute("UPDATE xtream_quality_queue SET state='cancelled',finished_at=?,last_error=NULL WHERE id=? AND state='pending'",
                     (time.time(), queue_id))


def next_request(conn):
    ensure_schema(conn)
    row = conn.execute("SELECT id,category_id,stream_id,stream_extension FROM xtream_quality_queue "
                       "WHERE state IN ('pending','running') ORDER BY requested_at,id LIMIT 1").fetchone()
    if row is None:
        return None
    return {'queue_id': row[0], 'id': None, 'category_id': row[1], 'stream_id': row[2], 'stream_extension': row[3]}


def recover_interrupted(conn):
    # Caller holds the cross-worker probe gate, with no surviving media lease.
    ensure_schema(conn)
    with conn:
        conn.execute("UPDATE xtream_quality_queue SET state='pending',started_at=NULL,"
                     "last_error='Previous check was interrupted; queued again' WHERE state='running'")


def mark(conn, queue_id, state, error=None):
    if queue_id is None:
        return
    with conn:
        conn.execute("UPDATE xtream_quality_queue SET state=?,last_error=?,"
                     "started_at=CASE WHEN ?='running' THEN ? ELSE started_at END,"
                     "finished_at=CASE WHEN ? IN ('completed','failed') THEN ? ELSE NULL END "
                     "WHERE id=? AND state IN ('pending','running')",
                     (state, error, state, time.time(), state, time.time(), queue_id))


def start(conn, request):
    channel = known_channel(conn, request['category_id'], request['stream_id'])
    with conn:
        changed = conn.execute("UPDATE xtream_quality_queue SET state='running',started_at=?,last_error=NULL "
                               "WHERE id=? AND state='pending'", (time.time(), request['queue_id'])).rowcount
    request['id'] = channel['id']
    request['stream_extension'] = channel['extension']
    return bool(changed)
