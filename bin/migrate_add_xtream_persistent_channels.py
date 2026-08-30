#!/usr/bin/env python3
"""Create the persistent Xtream channel table and indexes idempotently."""

import argparse
import sqlite3
from pathlib import Path

from server.services.xtream_persistent import ensure_schema


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args(argv)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(args.db))
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    print("Persistent Xtream channel schema ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
