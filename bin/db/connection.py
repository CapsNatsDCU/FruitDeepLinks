#!/usr/bin/env python3
"""
db/connection.py - SQLite connection management

Single entry point for all DB access.  Provides:
  - get_conn()  : context manager (with get_conn() as conn: ...)
  - resolve_db_path() : locate the database file
  - db_exists() : fast existence check
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


def resolve_db_path() -> Path:
    """Return the database path from the environment or fall back to default."""
    env_path = os.environ.get("FRUIT_DB_PATH") or os.environ.get("PEACOCK_DB_PATH")
    if env_path:
        return Path(env_path)
    # Docker default; local dev can set FRUIT_DB_PATH
    return Path("/app/data/fruit_events.db")


def db_exists() -> bool:
    return resolve_db_path().exists()


@contextmanager
def get_conn(db_path: Optional[Path] = None, row_factory=sqlite3.Row):
    """
    Context manager that yields an open SQLite connection and closes it on exit.

    Usage:
        with get_conn() as conn:
            rows = conn.execute("SELECT ...").fetchall()

    Args:
        db_path: Override the default database path (testing / multi-db use).
        row_factory: sqlite3 row factory.  Defaults to sqlite3.Row for dict-like access.
    """
    path = db_path or resolve_db_path()
    if not path.exists():
        raise FileNotFoundError(f"Database not found: {path}")

    # Read routes use a separate connection from the refresh writer.  The
    # timeout is only a small concurrency cushion; canonical materialization
    # is intentionally never performed here.
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = row_factory
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_conn_or_none(db_path: Optional[Path] = None, row_factory=sqlite3.Row):
    """
    Like get_conn() but yields None instead of raising if the DB doesn't exist.
    Useful in startup paths where the DB might not exist yet.
    """
    path = db_path or resolve_db_path()
    if not path.exists():
        yield None
        return

    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = row_factory
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
    finally:
        conn.close()
