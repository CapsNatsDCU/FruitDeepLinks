#!/usr/bin/env python3
"""Persistence, reconciliation, and export helpers for Xtream channels.

Provider credentials deliberately do not cross this module boundary.  Rows
contain only the provider identifiers required to reconstruct a stream URL at
tune time.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional
from urllib.parse import quote
from xml.etree import ElementTree as ET


PROVIDER = "xtream"
AVAILABILITY_VALUES = {"unknown", "available", "unavailable", "needs_attention"}
_CHANNEL_NUMBER_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_EXTENSION_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")


class PersistentChannelError(ValueError):
    """User-correctable persistent-channel validation failure."""


class DuplicatePersistentChannel(PersistentChannelError):
    pass


class ChannelNumberConflict(PersistentChannelError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_name(value: Any) -> str:
    """Normalize a provider name for conservative exact-name reconciliation."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = " ".join(text.split())
    return text.strip()


def normalize_channel_number(value: Any) -> str:
    text = str(value or "").strip()
    if not _CHANNEL_NUMBER_RE.fullmatch(text):
        raise PersistentChannelError(
            "Channel number must contain digits with an optional decimal point"
        )
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise PersistentChannelError("Invalid channel number") from exc
    if number < 0:
        raise PersistentChannelError("Channel number must not be negative")
    normalized = format(number.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized


def normalize_extension(value: Any) -> str:
    extension = str(value or "ts").strip().lstrip(".").lower()
    return extension if _EXTENSION_RE.fullmatch(extension) else "ts"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS xtream_persistent_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL DEFAULT 'xtream',
            enabled INTEGER NOT NULL DEFAULT 1,
            stream_id TEXT NOT NULL,
            category_id TEXT NOT NULL,
            category_name TEXT,
            original_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            channel_number TEXT NOT NULL,
            channel_id TEXT,
            guide_id TEXT,
            icon TEXT,
            logo_override TEXT,
            favorite_team TEXT,
            notes TEXT,
            stream_extension TEXT NOT NULL DEFAULT 'ts',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            availability_status TEXT NOT NULL DEFAULT 'unknown',
            unavailable_reason TEXT,
            last_checked_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # The table is new, but additive checks keep developer/preview databases
    # safe if they were created from an earlier iteration of the feature.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(xtream_persistent_channels)")}
    additions = {
        "category_name": "TEXT",
        "logo_override": "TEXT",
        "favorite_team": "TEXT",
        "notes": "TEXT",
        "availability_status": "TEXT NOT NULL DEFAULT 'unknown'",
        "unavailable_reason": "TEXT",
        "last_checked_at": "TEXT",
        "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    }
    for column, declaration in additions.items():
        if column not in existing:
            conn.execute(
                f"ALTER TABLE xtream_persistent_channels ADD COLUMN {column} {declaration}"
            )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_xtream_persistent_stream "
        "ON xtream_persistent_channels(provider, category_id, stream_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_xtream_persistent_channel_number "
        "ON xtream_persistent_channels(channel_number)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_xtream_persistent_enabled "
        "ON xtream_persistent_channels(enabled, availability_status)"
    )
    conn.commit()


def _clean_optional(value: Any, *, limit: int = 2048) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        raise PersistentChannelError("Text fields cannot contain control characters")
    return text[:limit]


def _stream_metadata(stream: Mapping[str, Any]) -> dict[str, Any]:
    """Keep a small credential-free provider snapshot, never a stream URL."""
    allowed = ("num", "tv_archive", "tv_archive_duration", "added")
    return {key: stream.get(key) for key in allowed if stream.get(key) is not None}


def _integrity_error(exc: sqlite3.IntegrityError) -> PersistentChannelError:
    message = str(exc).lower()
    if "channel_number" in message:
        return ChannelNumberConflict("That channel number is already in use")
    if "stream_id" in message or "ux_xtream_persistent_stream" in message:
        return DuplicatePersistentChannel("That Xtream stream is already configured")
    return PersistentChannelError("Persistent channel conflicts with an existing row")


def _row_to_dict(row: sqlite3.Row | tuple, columns: Optional[list[str]] = None) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        result = dict(row)
    else:
        result = dict(zip(columns or [], row))
    result["enabled"] = bool(result.get("enabled"))
    try:
        result["metadata"] = json.loads(result.pop("metadata_json", "{}") or "{}")
    except (TypeError, json.JSONDecodeError):
        result["metadata"] = {}
    result["logo"] = result.get("logo_override") or result.get("icon")
    result["effective_guide_id"] = (
        result.get("guide_id") or result.get("channel_id") or f"xtream.persistent.{result.get('id')}"
    )
    return result


def list_channels(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    ensure_schema(conn)
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM xtream_persistent_channels"
        params: tuple[Any, ...] = ()
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY CAST(channel_number AS REAL), channel_number, display_name"
        return [_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.row_factory = previous_factory


def get_channel(conn: sqlite3.Connection, channel_id: int) -> Optional[dict[str, Any]]:
    ensure_schema(conn)
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM xtream_persistent_channels WHERE id = ?", (channel_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.row_factory = previous_factory


def create_channel(
    conn: sqlite3.Connection,
    stream: Mapping[str, Any],
    *,
    category_id: Any,
    category_name: Any,
    channel_number: Any,
    display_name: Any = None,
    channel_id: Any = None,
    guide_id: Any = None,
    logo_override: Any = None,
    favorite_team: Any = None,
    notes: Any = None,
    enabled: bool = True,
) -> dict[str, Any]:
    ensure_schema(conn)
    stream_id = _clean_optional(stream.get("stream_id"), limit=255)
    original_name = _clean_optional(stream.get("name"), limit=1024)
    category_id_text = _clean_optional(category_id, limit=255)
    if not stream_id or not original_name or not category_id_text:
        raise PersistentChannelError("The selected stream is missing its name, stream ID, or category")
    display = _clean_optional(display_name, limit=1024) or original_name
    number = normalize_channel_number(channel_number)
    now = utc_now()
    epg_id = stream.get("epg_channel_id") or stream.get("epg_id")
    try:
        cursor = conn.execute(
            """
            INSERT INTO xtream_persistent_channels (
                provider, enabled, stream_id, category_id, category_name,
                original_name, display_name, channel_number, channel_id,
                guide_id, icon, logo_override, favorite_team, notes,
                stream_extension, metadata_json, availability_status,
                unavailable_reason, last_checked_at, created_at, updated_at
            ) VALUES ('xtream', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'available', NULL, ?, ?, ?)
            """,
            (
                1 if enabled else 0,
                stream_id,
                category_id_text,
                _clean_optional(category_name, limit=1024),
                original_name,
                display,
                number,
                _clean_optional(channel_id, limit=512),
                _clean_optional(guide_id, limit=512) or _clean_optional(epg_id, limit=512),
                _clean_optional(stream.get("stream_icon"), limit=2048),
                _clean_optional(logo_override, limit=2048),
                _clean_optional(favorite_team, limit=512),
                _clean_optional(notes, limit=4096),
                normalize_extension(stream.get("container_extension")),
                json.dumps(_stream_metadata(stream), separators=(",", ":")),
                now,
                now,
                now,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise _integrity_error(exc) from None
    return get_channel(conn, int(cursor.lastrowid))  # type: ignore[return-value]


_EDITABLE_FIELDS = {
    "display_name", "channel_number", "channel_id", "guide_id", "logo_override",
    "favorite_team", "notes", "enabled",
}


def update_channel(conn: sqlite3.Connection, persistent_id: int,
                   updates: Mapping[str, Any]) -> dict[str, Any]:
    ensure_schema(conn)
    current = get_channel(conn, persistent_id)
    if not current:
        raise KeyError("Persistent channel not found")
    assignments: list[str] = []
    values: list[Any] = []
    for key in _EDITABLE_FIELDS:
        if key not in updates:
            continue
        value = updates[key]
        if key == "enabled":
            value = 1 if value in (True, 1, "1", "true", "True") else 0
        elif key == "channel_number":
            value = normalize_channel_number(value)
        elif key == "display_name":
            value = _clean_optional(value, limit=1024)
            if not value:
                raise PersistentChannelError("Display name is required")
        elif key in {"channel_id", "guide_id", "favorite_team"}:
            value = _clean_optional(value, limit=512)
        elif key == "logo_override":
            value = _clean_optional(value, limit=2048)
        elif key == "notes":
            value = _clean_optional(value, limit=4096)
        assignments.append(f"{key} = ?")
        values.append(value)
    if not assignments:
        return current
    assignments.append("updated_at = ?")
    values.append(utc_now())
    values.append(persistent_id)
    try:
        conn.execute(
            f"UPDATE xtream_persistent_channels SET {', '.join(assignments)} WHERE id = ?",
            tuple(values),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise _integrity_error(exc) from None
    return get_channel(conn, persistent_id)  # type: ignore[return-value]


def delete_channel(conn: sqlite3.Connection, persistent_id: int) -> bool:
    ensure_schema(conn)
    cursor = conn.execute(
        "DELETE FROM xtream_persistent_channels WHERE id = ?", (persistent_id,)
    )
    conn.commit()
    return bool(cursor.rowcount)


def reconcile_channels(
    conn: sqlite3.Connection,
    streams_by_category: Mapping[str, list[dict]],
    category_names: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Reconcile saved IDs against an already-fetched configured-category snapshot."""
    ensure_schema(conn)
    channels = list_channels(conn)
    checked_at = utc_now()
    counts: dict[str, Any] = {
        "persistent_configured": len(channels),
        "persistent_available": 0,
        "persistent_reconciled": 0,
        "persistent_unavailable": 0,
        "persistent_reconciliations": [],
    }
    try:
        conn.execute("BEGIN")
        for channel in channels:
            category_id = str(channel["category_id"])
            rows = streams_by_category.get(category_id, [])
            current = [row for row in rows if str(row.get("stream_id")) == str(channel["stream_id"])]
            if current:
                row = current[0]
                conn.execute(
                    """
                    UPDATE xtream_persistent_channels
                       SET availability_status='available', unavailable_reason=NULL,
                           icon=COALESCE(?, icon), stream_extension=?, category_name=COALESCE(?, category_name),
                           last_checked_at=?, updated_at=?
                     WHERE id=?
                    """,
                    (
                        _clean_optional(row.get("stream_icon"), limit=2048),
                        normalize_extension(row.get("container_extension")),
                        (category_names or {}).get(category_id), checked_at, checked_at, channel["id"],
                    ),
                )
                counts["persistent_available"] += 1
                continue

            saved_name = normalize_name(channel["original_name"])
            matches = [row for row in rows if normalize_name(row.get("name")) == saved_name]
            replacement_conflict = False
            if len(matches) == 1:
                replacement = matches[0]
                new_stream_id = str(replacement.get("stream_id"))
                try:
                    conn.execute(
                        """
                        UPDATE xtream_persistent_channels
                           SET stream_id=?, stream_extension=?, icon=COALESCE(?, icon),
                               availability_status='available', unavailable_reason=NULL,
                               last_checked_at=?, updated_at=?
                         WHERE id=?
                        """,
                        (
                            new_stream_id,
                            normalize_extension(replacement.get("container_extension")),
                            _clean_optional(replacement.get("stream_icon"), limit=2048),
                            checked_at, checked_at, channel["id"],
                        ),
                    )
                except sqlite3.IntegrityError:
                    replacement_conflict = True
                else:
                    counts["persistent_available"] += 1
                    counts["persistent_reconciled"] += 1
                    counts["persistent_reconciliations"].append({
                        "id": channel["id"],
                        "old_stream_id": str(channel["stream_id"]),
                        "new_stream_id": new_stream_id,
                    })
                    continue

            if replacement_conflict:
                status = "needs_attention"
                reason = "The matching replacement stream is already configured"
            elif len(matches) > 1:
                status = "needs_attention"
                reason = "Multiple upstream streams exactly match the saved name"
            else:
                status = "unavailable"
                reason = "Saved stream was not found in its configured category"
            conn.execute(
                """
                UPDATE xtream_persistent_channels
                   SET availability_status=?, unavailable_reason=?, last_checked_at=?, updated_at=?
                 WHERE id=?
                """,
                (status, reason, checked_at, checked_at, channel["id"]),
            )
            counts["persistent_unavailable"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return counts


def _attribute(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_m3u(conn: sqlite3.Connection, server_url: str) -> str:
    base = str(server_url or "").rstrip("/")
    lines = ["#EXTM3U"]
    for channel in list_channels(conn, enabled_only=True):
        attrs = [
            f'tvg-id="{_attribute(channel["effective_guide_id"])}"',
            f'tvg-name="{_attribute(channel["display_name"])}"',
            f'tvg-chno="{_attribute(channel["channel_number"])}"',
            f'channel-number="{_attribute(channel["channel_number"])}"',
        ]
        if channel.get("logo"):
            attrs.append(f'tvg-logo="{_attribute(channel["logo"])}"')
        if channel.get("category_name"):
            attrs.append(f'group-title="{_attribute(channel["category_name"])}"')
        display_name = str(channel["display_name"]).replace("\r", " ").replace("\n", " ")
        lines.append(f"#EXTINF:-1 {' '.join(attrs)},{display_name}")
        lines.append(f"{base}/xtream/channel/{channel['id']}/stream")
    return "\n".join(lines) + "\n"


def render_xmltv(conn: sqlite3.Connection) -> bytes:
    root = ET.Element("tv", {"generator-info-name": "FruitDeepLinks"})
    for channel in list_channels(conn, enabled_only=True):
        element = ET.SubElement(root, "channel", {"id": str(channel["effective_guide_id"])})
        ET.SubElement(element, "display-name").text = str(channel["display_name"])
        if channel.get("logo"):
            ET.SubElement(element, "icon", {"src": str(channel["logo"])})
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def page_streams(streams: list[dict], query: str = "", page: int = 1,
                 page_size: int = 50) -> dict[str, Any]:
    needle = normalize_name(query)
    filtered = [
        stream for stream in streams
        if not needle or needle in normalize_name(stream.get("name"))
    ]
    filtered.sort(key=lambda row: normalize_name(row.get("name")))
    page = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))
    start = (page - 1) * page_size
    items = []
    for stream in filtered[start:start + page_size]:
        items.append({
            "stream_id": str(stream.get("stream_id")),
            "name": str(stream.get("name") or ""),
            "stream_icon": stream.get("stream_icon") or None,
            "epg_channel_id": stream.get("epg_channel_id") or stream.get("epg_id") or None,
            "container_extension": normalize_extension(stream.get("container_extension")),
        })
    return {"items": items, "total": len(filtered), "page": page, "page_size": page_size}
