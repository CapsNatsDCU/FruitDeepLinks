#!/usr/bin/env python3
"""
fruitdeeplinks_v2.py - Entrypoint for the v2 refactored server

Replaces the monolithic fruitdeeplinks_server.py.
Run directly:  python3 fruitdeeplinks_v2.py
Or via gunicorn: gunicorn 'fruitdeeplinks_v2:app'
"""

import os
import sys
from pathlib import Path

# Ensure bin/ is on sys.path for local helper imports
_BIN_DIR = str(Path(__file__).parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

def _apply_db_timezone() -> None:
    """Apply a DB-stored timezone setting to the process before the scheduler starts.

    Runs before create_app() so server.scheduler picks up the right TZ env var
    on its first (and only) BackgroundScheduler(timezone=...) call.
    """
    try:
        from db.connection import db_exists, get_conn
        from db.preferences import get_setting
        import time as _time

        if db_exists():
            with get_conn() as conn:
                tz = get_setting(conn, "timezone")
                if tz:
                    os.environ["TZ"] = tz
                    _time.tzset()
    except Exception:
        pass


_apply_db_timezone()

from server.app import create_app
from server.config import cfg
from server.logging_setup import log
from server.services.filters import get_auto_refresh
import server.scheduler as sched

app = create_app()

if __name__ == "__main__":
    try:
        from version_info import get_version
        version = get_version()
    except ImportError:
        version = "unknown"

    log(f"FruitDeepLinks v{version} starting (v2 server)", "INFO")

    port = int(os.getenv("PORT", 6655))
    host = os.getenv("HOST", "0.0.0.0")
    log(f"Listening on http://{host}:{port}", "INFO")

    # Start auto-refresh scheduler
    try:
        settings = get_auto_refresh()
        sched.start(settings)
    except Exception as e:
        log(f"Scheduler startup error: {e}", "WARNING")

    try:
        app.run(host=host, port=port, debug=False, threaded=True)
    finally:
        sched.stop()
