#!/usr/bin/env python3
"""
migrate_add_espn_locale_fallback.py - Add locale_fallback column to playables table

fix_espn_spanish_only.py rewrites the deeplink for ESPN events where Apple TV
only exposes a Spanish playable, pointing it at the general broadcast (via
espn_graph_id or externalId) instead of the Spanish-specific playID. That
repaired link is no longer Spanish-exclusive, but the playables.locale column
must stay 'es_MX' -- it's what lets fix_espn_spanish_only.py re-detect and
re-fix the row every day after Apple's re-scrape resets deeplink_play back to
the Spanish playID (community report: MLB Unlimited games showing zero valid
links under an English-only filter, since the repaired deeplink was still
being excluded as Spanish).

locale_fallback is a separate flag columns: fix_espn_spanish_only.py sets it
to 1 whenever it rewrites a Spanish-only playable, and filter_integration.py's
language filter treats a flagged playable as language-neutral (passes the
"en" filter) instead of excluding it as Spanish.

This migration:
1. Adds locale_fallback column to playables table (if missing)
2. No-op if column already exists
"""

import argparse
import sqlite3
from pathlib import Path


def ensure_locale_fallback_column(conn: sqlite3.Connection) -> bool:
    """Add locale_fallback column to playables table if it doesn't exist"""
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(playables)")
    columns = [row[1] for row in cur.fetchall()]

    if "locale_fallback" in columns:
        print("locale_fallback column already exists")
        return False

    print("Adding locale_fallback column to playables table...")
    cur.execute("ALTER TABLE playables ADD COLUMN locale_fallback INTEGER DEFAULT 0")
    conn.commit()
    print("locale_fallback column added successfully")
    return True


def main():
    ap = argparse.ArgumentParser(description="Add locale_fallback column to playables table")
    ap.add_argument("--db", default="data/fruit_events.db", help="Path to fruit_events.db")
    ap.add_argument("--yes", action="store_true", help="Auto-confirm without prompting")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    conn = sqlite3.connect(db_path)
    column_added = ensure_locale_fallback_column(conn)
    conn.close()

    print("\nMigration complete" if column_added else "\nNo changes needed (already migrated)")
    return 0


if __name__ == "__main__":
    exit(main())
