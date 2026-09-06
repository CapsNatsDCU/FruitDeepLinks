#!/usr/bin/env python3
"""Idempotently install the offline sports knowledge catalog schema."""
import argparse
import sqlite3

from sports_metadata import ensure_schema


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    with sqlite3.connect(args.db) as conn:
        ensure_schema(conn)
    print("sports knowledge catalog schema ready")


if __name__ == "__main__":
    main()
