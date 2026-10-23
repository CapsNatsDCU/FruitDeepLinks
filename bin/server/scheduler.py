#!/usr/bin/env python3
"""
server/scheduler.py - APScheduler setup for daily auto-refresh

Call start() once at app startup.  The scheduler reads/writes DB prefs
for the configured refresh time, so the DB must be accessible.
"""

import os
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

from server.config import cfg
from server.logging_setup import log
from server.refresh import run_refresh

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    _AVAILABLE = True
except ImportError:
    BackgroundScheduler = None
    _AVAILABLE = False

_scheduler = None
_job = None
_quality_job = None
_channel_sync_job = None
_channel_sync_attempts = 0


def _persistent_playlist_ready(expected_count: int) -> bool:
    """Wait until the running web server can export every saved channel."""
    port = int(os.getenv("PORT", "6655"))
    with urlopen(f"http://127.0.0.1:{port}/m3u/persistent", timeout=5) as response:
        return response.status == 200 and response.read().count(b"#EXTINF:") == expected_count


def _sync_channels_after_startup() -> None:
    """Reload the DVR source after both services have had time to start."""
    global _channel_sync_attempts, _channel_sync_job
    from db.connection import db_exists, get_conn
    from db.preferences import get_setting
    from server.services.channels_dvr_sync import refresh_source

    _channel_sync_attempts += 1
    done = False
    try:
        if not db_exists():
            raise RuntimeError("database is unavailable")
        with get_conn() as conn:
            # An empty/missing database must never replace a DVR lineup.
            count = conn.execute(
                "SELECT COUNT(*) FROM xtream_persistent_channels WHERE enabled=1"
            ).fetchone()[0]
            if not count:
                # A dataset restored after boot can make saved rows visible
                # later. Keep checking without sending an empty playlist.
                return
            host = str(get_setting(conn, "channels_dvr_ip") or "").strip()
            source = str(get_setting(conn, "channels_source_name") or "").strip()
        if not host or not source:
            done = True
            return
        if not _persistent_playlist_ready(count):
            raise RuntimeError("persistent playlist is not ready")
        results = refresh_source(host, source)
        failures = {key: value for key, value in results.items() if value is not None}
        if failures:
            raise RuntimeError(", ".join(f"{key}: {value}" for key, value in failures.items()))
        log(f"Channels DVR source reloaded after startup ({count} persistent channels)", "INFO")
        done = True
        return
    except Exception as exc:
        if _channel_sync_attempts <= 3 or _channel_sync_attempts % 12 == 0:
            log(f"Channels DVR startup reload attempt {_channel_sync_attempts} failed: {exc}", "WARNING")
    finally:
        if done:
            if _channel_sync_job and _scheduler:
                try:
                    _scheduler.remove_job(_channel_sync_job.id)
                except Exception:
                    pass  # Shutdown may have removed the running job already.
            _channel_sync_job = None


def _get_scheduler():
    return _scheduler


def start(auto_refresh_settings: dict | None = None) -> None:
    """Start APScheduler and schedule the auto-refresh job if enabled."""
    global _scheduler, _job, _quality_job, _channel_sync_job, _channel_sync_attempts

    if not _AVAILABLE:
        log("APScheduler not installed; auto-refresh disabled", "ERROR")
        return

    try:
        _scheduler = BackgroundScheduler(timezone=os.getenv("TZ", "America/New_York"))
        _scheduler.start()
        log("APScheduler started", "INFO")
        if auto_refresh_settings:
            schedule(auto_refresh_settings)
        _channel_sync_attempts = 0
        _channel_sync_job = _scheduler.add_job(
            func=_sync_channels_after_startup,
            trigger="interval",
            seconds=300,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
            id="channels_dvr_startup_reload",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        if os.getenv("XTREAM_BACKGROUND_QUALITY_ENABLED", "true").lower() not in {"0", "false", "no"}:
            from server.services.xtream_background_quality import run_background_quality
            _quality_job = _scheduler.add_job(
                func=run_background_quality,
                trigger="interval",
                seconds=5,
                next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
                id="xtream_background_quality",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=30,
            )
            log("Xtream resolution queue scheduled", "INFO")
    except Exception as e:
        log(f"Error starting APScheduler: {e}", "ERROR")


def schedule(settings: dict) -> None:
    """Apply (or remove) the auto-refresh cron job from `settings`."""
    global _job

    if not _scheduler:
        return

    enabled = bool(settings.get("enabled"))
    time_str = settings.get("time", "02:30")

    if not enabled:
        if _job:
            try:
                _scheduler.remove_job("daily_auto_refresh")
            except Exception:
                pass
            _job = None
        return

    try:
        hour, minute = [int(x) for x in time_str.split(":", 1)]
    except Exception:
        log(f"Invalid auto-refresh time: {time_str!r}", "ERROR")
        return

    _job = _scheduler.add_job(
        func=lambda: run_refresh(skip_scrape=False, source="auto"),
        trigger="cron",
        hour=hour,
        minute=minute,
        id="daily_auto_refresh",
        replace_existing=True,
        misfire_grace_time=2700,
        max_instances=1,
    )
    log(f"Auto-refresh scheduled daily at {hour:02d}:{minute:02d}", "INFO")


def next_run_iso() -> str | None:
    """Return the next scheduled run as an ISO string, or None."""
    if _job:
        try:
            return _job.next_run_time.isoformat()
        except Exception:
            pass
    return None


def stop() -> None:
    global _scheduler, _job, _quality_job, _channel_sync_job, _channel_sync_attempts
    if _scheduler:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
        _job = None
        _quality_job = None
        _channel_sync_job = None
        _channel_sync_attempts = 0
