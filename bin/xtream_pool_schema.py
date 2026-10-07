"""Additive, secret-free middleware schema. Safe on existing Fruit databases."""
import sqlite3


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS xtream_account_state (
            account_id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            label_override TEXT,
            enabled_override INTEGER,
            capacity_override INTEGER,
            discovered_capacity INTEGER,
            health TEXT NOT NULL DEFAULT 'unknown',
            last_checked REAL,
            last_success REAL,
            last_error TEXT,
            retry_after REAL NOT NULL DEFAULT 0,
            exclusive_for_background INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS xtream_leases (
            lease_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            stream_id TEXT NOT NULL,
            source TEXT NOT NULL,
            started REAL NOT NULL,
            fingerprint TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ix_xtream_leases_account ON xtream_leases(account_id);
        CREATE TABLE IF NOT EXISTS xtream_stream_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT,
            stream_id TEXT,
            source TEXT,
            started REAL,
            ended REAL NOT NULL,
            outcome TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS xtream_epg_programmes (
            persistent_id INTEGER NOT NULL,
            stream_id TEXT NOT NULL,
            guide_id TEXT NOT NULL,
            start_utc TEXT NOT NULL,
            stop_utc TEXT NOT NULL,
            programme_xml TEXT NOT NULL,
            PRIMARY KEY(persistent_id,start_utc,stop_utc)
        );
        CREATE TABLE IF NOT EXISTS xtream_epg_status (
            persistent_id INTEGER PRIMARY KEY,
            checked_at TEXT NOT NULL,
            last_success TEXT,
            programme_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS channels_lane_numbers (
            lane_id INTEGER PRIMARY KEY,
            channel_number TEXT NOT NULL UNIQUE
        );
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(xtream_leases)")}
    if "fingerprint" not in columns:
        conn.execute("ALTER TABLE xtream_leases ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE xtream_leases SET fingerprint=COALESCE((SELECT fingerprint FROM xtream_account_state a WHERE a.account_id=xtream_leases.account_id),'')")
    account_columns = {row[1] for row in conn.execute("PRAGMA table_info(xtream_account_state)")}
    if "exclusive_for_background" not in account_columns:
        conn.execute("ALTER TABLE xtream_account_state ADD COLUMN exclusive_for_background INTEGER NOT NULL DEFAULT 0")
    conn.commit()
