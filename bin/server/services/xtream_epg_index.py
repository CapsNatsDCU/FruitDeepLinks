"""Offline index of provider XMLTV channel identities, without feed credentials."""

from __future__ import annotations

from difflib import SequenceMatcher
import json
import re
import sqlite3
from typing import Any

from server.services.xtream_persistent import list_channels, normalize_name, utc_now
from xtream_accounts import safe_value


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS xtream_epg_link_index (
            guide_id TEXT PRIMARY KEY,
            display_names_json TEXT NOT NULL,
            programme_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS xtream_epg_link_index_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            refreshed_at TEXT NOT NULL,
            channel_count INTEGER NOT NULL
        );
    """)


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        row = conn.execute("SELECT refreshed_at,channel_count FROM xtream_epg_link_index_state WHERE id=1").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        row = None
    from server.services.external_xmltv import status as external_status
    external = external_status(conn)
    provider = dict(zip(("refreshed_at", "channel_count"), row)) if row else {"refreshed_at": None, "channel_count": 0}
    return {"refreshed_at": max(provider["refreshed_at"] or "", external.get("refreshed_at") or "") or None,
            "channel_count": provider["channel_count"] + external.get("channel_count", 0)}


def replace_snapshot(conn: sqlite3.Connection, discovered: dict, accounts=()) -> dict[str, Any]:
    """Replace only after the complete XMLTV document has parsed successfully."""
    accounts = list(accounts)
    rows = []
    for raw_id, item in discovered.items():
        guide_id = str(raw_id or "").strip()[:512]
        if not guide_id or safe_value(guide_id, accounts) != guide_id:
            continue
        names = []
        for value in item.get("names", [])[:8]:
            name = safe_value(str(value or "").strip()[:256], accounts)
            if name and name not in names:
                names.append(name)
        if names:
            rows.append((guide_id, json.dumps(names, ensure_ascii=False),
                         max(0, int(item.get("programme_count") or 0))))
    if not rows:
        raise ValueError("Provider XMLTV has no usable channel definitions; existing EPG index was kept")
    refreshed_at = utc_now()
    with conn:
        ensure_schema(conn)
        conn.execute("DELETE FROM xtream_epg_link_index")
        conn.executemany("INSERT INTO xtream_epg_link_index VALUES (?,?,?)", rows)
        conn.execute("INSERT INTO xtream_epg_link_index_state VALUES (1,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET refreshed_at=excluded.refreshed_at,channel_count=excluded.channel_count",
                     (refreshed_at, len(rows)))
    return {"refreshed_at": refreshed_at, "channel_count": len(rows)}


def _provider_entries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute("SELECT guide_id,display_names_json,programme_count FROM xtream_epg_link_index ORDER BY guide_id")
        return [{"guide_id": row[0], "display_names": json.loads(row[1]),
                 "programme_count": row[2]} for row in rows]
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return []


def entries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    from server.services.external_xmltv import entries as external_entries
    return _provider_entries(conn) + external_entries(conn)


_NOISE = {"raw", "hd", "fhd", "uhd", "sd", "4k", "1080p", "720p"}


def _match_name(value: str) -> str:
    text = normalize_name(value)
    text = re.sub(r"^[^:]{1,12}:\s*", "", text)
    tokens = re.findall(r"\w+", text, re.UNICODE)
    return " ".join(token for token in tokens if token not in _NOISE and not re.fullmatch(r"\d+fps", token))


def search(conn: sqlite3.Connection, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Name suggestions only; read cached IDs without fetching or assigning."""
    from server.services.xtream_channel_cache import all_channels
    source_name = _match_name(query)
    if not source_name:
        return []
    source_tokens = set(source_name.split())
    source_numbers = set(re.findall(r"\d+", source_name))
    indexed = {item["guide_id"]: item for item in entries(conn)}
    names = [(item["guide_id"], name, item.get("source", "Provider XMLTV feed"))
             for item in indexed.values() for name in item["display_names"]]
    names += [(item["epg_channel_id"], item["name"], item["category_name"])
              for item in all_channels(conn) if item.get("epg_channel_id")]
    ranked = {}
    for guide_id, name, source in names:
        candidate = _match_name(name)
        if not candidate:
            continue
        tokens = set(candidate.split())
        candidate_numbers = set(re.findall(r"\d+", candidate))
        if source_numbers and candidate_numbers and source_numbers != candidate_numbers:
            continue
        shared = len(source_tokens & tokens)
        score = max(SequenceMatcher(None, source_name, candidate).ratio(),
                    shared / len(source_tokens | tokens))
        # A short station query should find its longer provider label.
        if shared == len(source_tokens):
            score = max(score, 0.95)
        elif len(source_tokens) > 1 and shared / len(source_tokens) >= 0.75:
            score = max(score, 0.9 * shared / len(source_tokens))
        if score < 0.65 or score <= ranked.get(guide_id, {}).get("similarity", -1):
            continue
        ranked[guide_id] = {"guide_id": guide_id, "display_name": name,
                            "similarity": score, "source": source,
                            "provider_programmes": indexed[guide_id]["programme_count"] if guide_id in indexed else None}
    result = sorted(ranked.values(), key=lambda item: (-item["similarity"], item["display_name"].casefold(), item["guide_id"]))
    for item in result:
        item["similarity"] = round(item["similarity"], 2)
    return result[:max(1, min(25, limit))]


def suggestions(conn: sqlite3.Connection, *, limit: int = 5) -> list[dict[str, Any]]:
    """Offer name-only candidates for saved channels lacking cached programmes."""
    from xtream_epg import cached_programmes
    indexed = [item for item in entries(conn) if item["programme_count"] > 0]
    result = []
    for channel in list_channels(conn, enabled_only=True):
        if cached_programmes(conn, channel):
            continue
        source_name = _match_name(channel["display_name"])
        if not source_name:
            continue
        source_tokens = set(source_name.split())
        ranked = []
        for entry in indexed:
            score = 0.0
            matched_name = None
            for name in entry["display_names"]:
                candidate = _match_name(name)
                if not candidate:
                    continue
                tokens = set(candidate.split())
                overlap = len(source_tokens & tokens) / len(source_tokens | tokens)
                current = max(SequenceMatcher(None, source_name, candidate).ratio(), overlap)
                if current > score:
                    score, matched_name = current, name
            if score >= 0.65 and matched_name:
                ranked.append({"guide_id": entry["guide_id"], "display_name": matched_name,
                               "similarity": round(score, 2),
                               "provider_programmes": entry["programme_count"], "source": entry.get("source", "Provider XMLTV feed")})
        ranked.sort(key=lambda item: (-item["similarity"], item["display_name"].casefold(), item["guide_id"]))
        result.append({"persistent_id": channel["id"], "channel_name": channel["display_name"],
                       "current_epg_channel_id": channel.get("epg_source_id") or channel.get("epg_channel_id"),
                       "candidates": ranked[:max(1, min(10, limit))]})
    return result
