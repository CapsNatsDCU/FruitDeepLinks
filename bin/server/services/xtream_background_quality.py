"""Slow, fail-closed resolution checks for saved Xtream channels.

The provider's active_cons value is a snapshot, not an atomic reservation.
Every background sample also holds Fruit's account lease and requires two
fresh zero readings. Missing activity data always skips media.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from db.connection import resolve_db_path
from server.logging_setup import log
from server.refresh import refresh_status
from server.services.xtream_persistent import ensure_schema, save_stream_quality
from server.services.xtream_quality import (
    QualityProbeDeferred, _probe_bytes, _require_quiet, _sample_media, quality_probe_guard,
)
from server.services import xtream_quality_queue as quality_queue
from update_protocol import installation_active
from xtream_ingest import XtreamClient
from xtream_pool import PoolUnavailable, XtreamPool


ACCOUNT_INTERVAL_SECONDS = 10
CHANNEL_INTERVAL_SECONDS = 600
ACTIVITY_RECHECK_SECONDS = 2


def _ensure_state(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS xtream_background_quality_accounts (
        account_id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        last_attempt REAL NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS xtream_background_quality_channels (
        channel_id INTEGER PRIMARY KEY,
        last_attempt REAL NOT NULL
    )""")


def _next_channel(conn, now):
    ensure_schema(conn)
    _ensure_state(conn)
    queued = quality_queue.next_request(conn)
    if queued is not None:
        return queued
    candidates = automatic_candidates(conn, now, limit=1)
    return candidates[0] if candidates else None


def automatic_candidates(conn, now, *, limit=25):
    ensure_schema(conn)
    _ensure_state(conn)
    cursor = conn.execute("""
        SELECT c.id, c.category_id, c.stream_id, c.stream_extension, c.display_name,
               (attempted.last_attempt IS NULL) AS first_check
        FROM xtream_persistent_channels AS c
        LEFT JOIN xtream_stream_quality AS q
          ON q.category_id=c.category_id AND q.stream_id=c.stream_id
        LEFT JOIN xtream_background_quality_channels AS attempted ON attempted.channel_id=c.id
        WHERE c.enabled=1 AND c.availability_status='available'
          AND (attempted.last_attempt IS NULL OR attempted.last_attempt<=?)
        ORDER BY (attempted.last_attempt IS NOT NULL),
                 CASE WHEN attempted.last_attempt IS NULL THEN c.created_at END DESC,
                 CASE WHEN attempted.last_attempt IS NULL THEN c.id END DESC,
                 (q.measured_at IS NOT NULL), COALESCE(attempted.last_attempt,0),
                 q.measured_at, c.id
        LIMIT ?
    """, (now - CHANNEL_INTERVAL_SECONDS, limit))
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor]


def _account_due(conn, account, now):
    row = conn.execute(
        "SELECT fingerprint,last_attempt FROM xtream_background_quality_accounts WHERE account_id=?",
        (account.id,),
    ).fetchone()
    return row is None or row[0] != account.fingerprint or row[1] <= now - ACCOUNT_INTERVAL_SECONDS


def _mark_account_attempt(conn, account, now):
    conn.execute("""INSERT INTO xtream_background_quality_accounts(account_id,fingerprint,last_attempt)
        VALUES(?,?,?) ON CONFLICT(account_id) DO UPDATE SET
        fingerprint=excluded.fingerprint,last_attempt=excluded.last_attempt""",
        (account.id, account.fingerprint, now))


def _all_other_activity_absent(pool, account_id):
    status = pool.status()
    return (status["active"] == 1 and
            any(row["id"] == account_id and row["reserved_for_fruit"] for row in status["accounts"]) and
            all(row["active"] == (1 if row["id"] == account_id else 0)
                and (row["id"] == account_id or not row["busy"])
                for row in status["accounts"]))


def _run_background_quality(db_path: Path | None = None, *, pool=None) -> str:
    """Make at most one conservative attempt per tick, with no network on skip."""
    if os.getenv("NO_NETWORK", "false").lower() in {"1", "true", "yes"}:
        return "offline"
    if installation_active() or refresh_status["running"]:
        return "service_busy"
    path = Path(db_path or resolve_db_path())
    if not path.is_file():
        return "no_database"
    pool = pool or XtreamPool(path)
    status = pool.status()
    if status["active"] or any(row["busy"] for row in status["accounts"]):
        return "local_activity"
    eligible_ids = {row["id"] for row in status["accounts"]
                    if row["reserved_for_fruit"] and row["enabled"] and
                    row["health"] == "healthy" and row["available"] > 0}
    if not eligible_ids:
        return "no_reserved_healthy_account"
    now = time.time()
    with pool.connection() as conn:
        _ensure_state(conn)
        candidate = _next_channel(conn, now)
        if candidate is None:
            return "no_due_channel"
        if not any(_account_due(conn, account, now) for account in pool.accounts
                   if account.id in eligible_ids):
            return "account_interval"

    try:
        with quality_probe_guard(path, pool=pool):
            if installation_active() or refresh_status["running"]:
                return "service_busy"
            status = pool.status()
            if status["active"] or any(row["busy"] for row in status["accounts"]):
                return "local_activity"
            now = time.time()
            with pool.connection() as conn:
                _ensure_state(conn)
                quality_queue.recover_interrupted(conn)
                channel = _next_channel(conn, now)
                if channel is None:
                    return "no_due_channel"
                accounts = [account for account in pool.accounts if account.id in eligible_ids
                            and _account_due(conn, account, now)]
            if not accounts:
                return "account_interval"
            # Stable rotation across accounts follows oldest attempt first.
            with pool.connection() as conn:
                accounts.sort(key=lambda account: (conn.execute(
                    "SELECT last_attempt FROM xtream_background_quality_accounts WHERE account_id=?",
                    (account.id,),
                ).fetchone() or (0,))[0])
            account = accounts[0]
            queue_id = channel.get("queue_id") if isinstance(channel, dict) else None
            try:
                lease = pool.acquire(channel["stream_id"], "quality_probe",
                                     excluded={item.id for item in pool.accounts if item.id != account.id})
            except PoolUnavailable:
                return "account_became_busy"
            outcome = "client_closed"
            try:
                if not _all_other_activity_absent(pool, account.id):
                    return "local_activity"
                with pool.connection() as conn:
                    _mark_account_attempt(conn, account, time.time())
                client = XtreamClient(lease.account.config, timeout=3)
                try:
                    _require_quiet(path)
                    first = client.get_probe_active_connections(lease.gate_fd)
                    if first != 0:
                        with pool.connection() as conn:
                            quality_queue.mark(conn, queue_id, 'pending', 'Provider account is occupied or its activity is unknown')
                        return "provider_occupied_or_unknown"
                    time.sleep(ACTIVITY_RECHECK_SECONDS)
                    if (installation_active() or refresh_status["running"] or
                            not _all_other_activity_absent(pool, account.id)):
                        return "local_activity"
                    _require_quiet(path)
                    second = client.get_probe_active_connections(lease.gate_fd)
                    if second != 0:
                        with pool.connection() as conn:
                            quality_queue.mark(conn, queue_id, 'pending', 'Provider account is occupied or its activity is unknown')
                        return "provider_occupied_or_unknown"
                finally:
                    client.session.close()
                with pool.connection() as conn:
                    if queue_id is not None:
                        try:
                            if not quality_queue.start(conn, channel):
                                return 'queue_cancelled'
                        except Exception:
                            quality_queue.mark(conn, queue_id, 'failed', 'The selected channel is no longer available in the saved catalog')
                            return 'queue_unavailable'
                    if channel['id'] is not None:
                        conn.execute("""INSERT INTO xtream_background_quality_channels(channel_id,last_attempt)
                        VALUES(?,?) ON CONFLICT(channel_id) DO UPDATE SET last_attempt=excluded.last_attempt""",
                        (channel["id"], time.time()))
                _require_quiet(path)
                sample = _sample_media(lease, channel["stream_id"], channel["stream_extension"])
            except QualityProbeDeferred:
                with pool.connection() as conn:
                    quality_queue.mark(conn, queue_id, 'pending', 'Paused for channel or account activity')
                raise
            except Exception:
                outcome = "upstream_error"
                with pool.connection() as conn:
                    quality_queue.mark(conn, queue_id, 'failed', 'Resolution check failed; choose Queue test to retry')
                raise
            finally:
                lease.release(outcome)
            try:
                measured = _probe_bytes(sample)
                with pool.connection() as conn:
                    save_stream_quality(conn, channel["category_id"], channel["stream_id"], measured)
                    quality_queue.mark(conn, queue_id, 'completed')
            except Exception:
                with pool.connection() as conn:
                    quality_queue.mark(conn, queue_id, 'failed', 'The sample had no usable video resolution; choose Queue test to retry')
                raise
            log(f"Background Xtream resolution measured persistent channel {channel['id']} using {account.id}", "INFO")
            return "measured"
    except (QualityProbeDeferred, PoolUnavailable):
        return "deferred"


def run_background_quality(db_path: Path | None = None, *, pool=None) -> str:
    """Scheduler entrypoint: no provider exception can kill future ticks."""
    try:
        return _run_background_quality(db_path, pool=pool)
    except Exception as exc:
        # Exception text can contain authenticated URLs; only its class is safe.
        log(f"Background Xtream quality check skipped: {type(exc).__name__}", "WARNING")
        return "error"
