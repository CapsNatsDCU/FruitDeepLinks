"""Saved, credential-safe Xtream channel catalog for provider-free browsing."""

from __future__ import annotations

import sqlite3
from typing import Any

from server.services.xtream_persistent import advertised_quality, normalize_extension, normalize_name, utc_now
from xtream_accounts import safe_value
from xtream_ingest import XtreamError


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS xtream_channel_cache_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            refreshed_at TEXT NOT NULL,
            category_count INTEGER NOT NULL,
            stream_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS xtream_channel_cache_categories (
            category_id TEXT PRIMARY KEY,
            category_name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS xtream_channel_cache_streams (
            category_id TEXT NOT NULL,
            stream_id TEXT NOT NULL,
            name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            stream_icon TEXT,
            epg_channel_id TEXT,
            container_extension TEXT NOT NULL,
            PRIMARY KEY (category_id, stream_id)
        );
        CREATE INDEX IF NOT EXISTS idx_xtream_channel_cache_name
            ON xtream_channel_cache_streams(normalized_name, category_id, stream_id);
    """)


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        row = conn.execute("SELECT refreshed_at,category_count,stream_count FROM xtream_channel_cache_state WHERE id=1").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        row = None
    return dict(row) if row else {"refreshed_at": None, "category_count": 0, "stream_count": 0}


def fetch_snapshot(client, accounts) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, str, str | None, str | None, str]]]:
    """Fetch once for all streams; use per-category calls only if necessary."""
    upstream_categories = client.get_live_categories()
    categories = {}
    for row in upstream_categories:
        category_id = str(row.get("category_id") or "").strip()
        if category_id and safe_value(category_id, accounts) == category_id:
            categories[category_id] = str(row.get("category_name") or f"Category {category_id}").strip()
    if not categories:
        raise XtreamError("Provider returned no categories; existing channel cache was kept")

    try:
        streams = client.get_all_live_streams()
    except XtreamError:
        streams = []
    if not streams or any(not str(row.get("category_id") or "").strip() for row in streams):
        streams = [
            {**row, "category_id": category_id}
            for category_id in sorted(categories)
            for row in client.get_live_streams(category_id)
        ]
    if not streams:
        raise XtreamError("Provider returned no channels; existing channel cache was kept")

    category_rows = [(category_id, safe_value(name, accounts)) for category_id, name in sorted(categories.items())]
    entries = {}
    for row in streams:
        category_id = str(row.get("category_id") or "").strip()
        stream_id = str(row.get("stream_id") or "").strip()
        if (category_id not in categories or not stream_id
                or safe_value(stream_id, accounts) != stream_id):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        projected = safe_value({
            "name": name,
            "stream_icon": str(row.get("stream_icon") or "")[:2048] or None,
            "epg_channel_id": str(row.get("epg_channel_id") or row.get("epg_id") or "")[:256] or None,
        }, accounts)
        clean_name = projected["name"][:512]
        entries[(category_id, stream_id)] = (
            category_id, stream_id, clean_name, normalize_name(clean_name),
            projected["stream_icon"], projected["epg_channel_id"],
            normalize_extension(row.get("container_extension")),
        )
    if not entries:
        raise XtreamError("Provider returned no usable channels; existing channel cache was kept")
    return category_rows, list(entries.values())


def replace_snapshot(conn: sqlite3.Connection, categories, streams) -> dict[str, Any]:
    """Replace the entire snapshot in one transaction after provider work ends."""
    refreshed_at = utc_now()
    with conn:
        ensure_schema(conn)
        conn.execute("DELETE FROM xtream_channel_cache_streams")
        conn.execute("DELETE FROM xtream_channel_cache_categories")
        conn.executemany("INSERT INTO xtream_channel_cache_categories VALUES (?,?)", categories)
        conn.executemany("INSERT INTO xtream_channel_cache_streams VALUES (?,?,?,?,?,?,?)", streams)
        conn.execute("INSERT INTO xtream_channel_cache_state VALUES (1,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET refreshed_at=excluded.refreshed_at,category_count=excluded.category_count,stream_count=excluded.stream_count",
                     (refreshed_at, len(categories), len(streams)))
    return {"refreshed_at": refreshed_at, "category_count": len(categories), "stream_count": len(streams)}


def _stream(row) -> dict[str, Any]:
    item = dict(row)
    item["advertised_quality"] = advertised_quality(item["name"])
    return item


def all_channels(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute("SELECT s.category_id,c.category_name,s.stream_id,s.name,s.stream_icon,s.epg_channel_id,s.container_extension "
                            "FROM xtream_channel_cache_streams s JOIN xtream_channel_cache_categories c USING(category_id) "
                            "ORDER BY s.normalized_name,s.category_id,s.stream_id")
        return [_stream(row) for row in rows]
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return []


def get_stream(conn: sqlite3.Connection, category_id: str, stream_id: str) -> dict[str, Any] | None:
    try:
        row = conn.execute("SELECT s.category_id,c.category_name,s.stream_id,s.name,s.stream_icon,s.epg_channel_id,s.container_extension "
                           "FROM xtream_channel_cache_streams s JOIN xtream_channel_cache_categories c USING(category_id) "
                           "WHERE s.category_id=? AND s.stream_id=?", (category_id, stream_id)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        row = None
    return _stream(row) if row else None


def search(conn: sqlite3.Connection, query: str, scope: str, selected: set[str], page: int, page_size: int) -> dict[str, Any]:
    page, page_size = max(1, page), min(100, max(1, page_size))
    if scope == "active" and not selected:
        return {"items": [], "total": 0, "page": page, "page_size": page_size, "category_count": 0}
    needle = normalize_name(query).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    where = "WHERE s.normalized_name LIKE ? ESCAPE '\\'"
    params: list[Any] = [f"%{needle}%"]
    category_where = ""
    category_params: list[Any] = []
    if scope == "active":
        placeholders = ",".join("?" for _ in selected)
        where += f" AND s.category_id IN ({placeholders})"
        params.extend(sorted(selected))
        category_where = f"WHERE category_id IN ({placeholders})"
        category_params.extend(sorted(selected))
    category_count = conn.execute(f"SELECT COUNT(*) FROM xtream_channel_cache_categories {category_where}", category_params).fetchone()[0]
    source = "FROM xtream_channel_cache_streams s JOIN xtream_channel_cache_categories c USING(category_id) "
    total = conn.execute(f"SELECT COUNT(*) {source} {where}", params).fetchone()[0]
    rows = conn.execute(f"SELECT s.category_id,c.category_name,s.stream_id,s.name,s.stream_icon,s.epg_channel_id,s.container_extension "
                        f"{source} {where} ORDER BY s.normalized_name,s.category_id,s.stream_id LIMIT ? OFFSET ?",
                        [*params, page_size, (page - 1) * page_size]).fetchall()
    return {"items": [_stream(row) for row in rows], "total": total, "page": page,
            "page_size": page_size, "category_count": category_count}
