#!/usr/bin/env python3
"""Audit Xtream category context on a local Fruit SQLite copy, without provider traffic.

The report is evidence for operator review, never authority to promote a
category. Title and EPG text may themselves be mislabeled, so even a clean
report does not create a classification mapping.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

from sports_metadata import _context_from_text, _json, normalize_sport


def audit_categories(conn: sqlite3.Connection) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "names": set(), "observations": 0, "title_leagues": defaultdict(int),
        "title_sports": defaultdict(int), "category_leagues": defaultdict(int),
        "category_sports": defaultdict(int), "league_conflicts": 0,
        "sport_conflicts": 0, "without_title_context": 0,
    })
    for (raw_text,) in conn.execute("SELECT raw_json FROM source_event_records WHERE source='xtream'"):
        raw = _json(raw_text)
        if not isinstance(raw, dict):
            continue
        category_id = str(raw.get("category_id") or "").strip()
        category_name = str(raw.get("category_name") or "").strip()
        if not category_id and not category_name:
            continue
        key = category_id or f"name:{category_name.casefold()}"
        item = grouped[key]
        item["names"].add(category_name)
        item["observations"] += 1
        title = str(raw.get("epg_title") or raw.get("original_stream_name") or "")
        description = str(raw.get("epg_description") or "")
        title_sport, title_league = _context_from_text(f"{title} {description}")
        category_sport, category_league = _context_from_text(category_name)
        if title_sport:
            item["title_sports"][title_sport] += 1
        if title_league:
            item["title_leagues"][title_league] += 1
        if category_sport:
            item["category_sports"][category_sport] += 1
        if category_league:
            item["category_leagues"][category_league] += 1
        if not title_sport and not title_league:
            item["without_title_context"] += 1
        if title_sport and category_sport and normalize_sport(title_sport) != normalize_sport(category_sport):
            item["sport_conflicts"] += 1
        if title_league and category_league and title_league != category_league:
            item["league_conflicts"] += 1
    return {"categories": {
        key: {**{name: value for name, value in item.items() if name not in
                 {"names", "title_leagues", "title_sports", "category_leagues", "category_sports"}},
              "names": sorted(item["names"]),
              **{name: dict(sorted(item[name].items())) for name in
                 ("title_leagues", "title_sports", "category_leagues", "category_sports")}}
        for key, item in sorted(grouped.items())},
        "promotion_authorized": False}


def compare_audits(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    before, after = first["categories"], second["categories"]
    shared = set(before) & set(after)
    return {"new_category_ids": sorted(set(after) - set(before)),
            "missing_category_ids": sorted(set(before) - set(after)),
            "renamed_category_ids": sorted(key for key in shared if before[key]["names"] != after[key]["names"]),
            "changed_title_league_evidence": sorted(key for key in shared
                                                    if set(before[key]["title_leagues"]) != set(after[key]["title_leagues"])),
            "promotion_authorized": False}


def _open_copy(path: Path) -> sqlite3.Connection:
    if not path.is_file() or not path.stat().st_size:
        raise ValueError("database copy must be a nonempty local SQLite file")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_event_records'").fetchone():
        conn.close()
        raise ValueError("database copy is missing source_event_records")
    return conn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-copy", required=True, type=Path, help="local Fruit SQLite copy")
    parser.add_argument("--compare-db-copy", type=Path, help="optional second copy to check category drift")
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    args = parser.parse_args()
    with closing(_open_copy(args.db_copy)) as conn:
        report = audit_categories(conn)
    if args.compare_db_copy:
        with closing(_open_copy(args.compare_db_copy)) as conn:
            newer = audit_categories(conn)
        report["comparison"] = compare_audits(report, newer)
        report["newer"] = newer
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
