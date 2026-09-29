"""Idempotently add account-state, lease, EPG and lineup tables; no secrets."""
import argparse
import sqlite3
from pathlib import Path

from server.services.xtream_persistent import ensure_schema as persistent_schema
from xtream_pool_schema import ensure_schema


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.db) as conn:
        persistent_schema(conn)
        ensure_schema(conn)
    print("Xtream pool and guide schema ready; existing configuration preserved")


if __name__ == "__main__":
    main()
