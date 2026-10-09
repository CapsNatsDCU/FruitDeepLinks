"""One operator-configured XMLTV source, independent of IPTV credentials.

Download and parse into a disk-backed staging database before atomically
replacing the saved snapshot. Channel lookups and exports remain offline.
"""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import requests

PREFIX = "xmltv:"
MAX_BYTES = 128 * 1024 * 1024


def ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS external_xmltv_state (
            id INTEGER PRIMARY KEY CHECK(id=1), url TEXT NOT NULL,
            refreshed_at TEXT NOT NULL, checked_at TEXT NOT NULL,
            channel_count INTEGER NOT NULL, programme_count INTEGER NOT NULL,
            last_error TEXT);
        CREATE TABLE IF NOT EXISTS external_xmltv_channels (
            guide_id TEXT PRIMARY KEY, names_json TEXT NOT NULL,
            programme_count INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS external_xmltv_programmes (
            guide_id TEXT NOT NULL, start_utc TEXT NOT NULL, stop_utc TEXT NOT NULL,
            programme_xml TEXT NOT NULL, PRIMARY KEY(guide_id,start_utc,stop_utc));
    """)


def status(conn):
    try:
        row = conn.execute("SELECT url,refreshed_at,checked_at,channel_count,programme_count,last_error "
                           "FROM external_xmltv_state WHERE id=1").fetchone()
        return dict(zip(("url", "refreshed_at", "checked_at", "channel_count", "programme_count", "last_error"), row)) if row else {}
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return {}


def entries(conn):
    if not status(conn):
        return []
    return [{"guide_id": PREFIX + row[0], "display_names": json.loads(row[1]),
             "programme_count": row[2], "source": "External XMLTV / zap2xml"}
            for row in conn.execute("SELECT guide_id,names_json,programme_count FROM external_xmltv_channels ORDER BY guide_id")]


def programmes(conn, source_id):
    if not status(conn):
        return []
    now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S +0000")
    return [ET.fromstring(row[0]) for row in conn.execute(
        "SELECT programme_xml FROM external_xmltv_programmes WHERE guide_id=? AND stop_utc>? ORDER BY start_utc LIMIT 10000",
        (source_id[len(PREFIX):], now))]


def browse(conn, query="", *, offset=0, limit=50):
    """Browse only the imported snapshot and show its current channel assignments."""
    from server.services.xtream_persistent import list_channels
    query = query.casefold().strip()
    assigned = {}
    for channel in list_channels(conn):
        assigned.setdefault(channel.get("epg_source_id"), []).append({
            "id": channel["id"], "display_name": channel["display_name"],
            "channel_number": channel["channel_number"], "enabled": channel["enabled"]})
    stations = [{**station, "assigned_channels": assigned.get(station["guide_id"], [])}
                for station in entries(conn)
                if not query or query in station["guide_id"].casefold()
                or any(query in name.casefold() for name in station["display_names"])]
    stations.sort(key=lambda s: (s["display_names"][0].casefold(), s["guide_id"]))
    return {"stations": stations[offset:offset + limit], "total": len(stations),
            "offset": offset, "limit": limit, "has_more": offset + limit < len(stations)}


def station_preview(conn, guide_id):
    """Return a bounded, text-only current/upcoming schedule from the saved file."""
    from xtream_epg import xml_time
    station = next((s for s in entries(conn) if s["guide_id"] == guide_id), None)
    if station is None:
        raise KeyError("Imported XMLTV station not found")
    now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S +0000")
    rows = conn.execute("SELECT programme_xml FROM external_xmltv_programmes "
                        "WHERE guide_id=? AND stop_utc>? ORDER BY start_utc LIMIT 20",
                        (guide_id[len(PREFIX):], now))
    schedule = []
    for row in rows:
        p = ET.fromstring(row[0])
        schedule.append({"title": p.findtext("title") or "", "description": p.findtext("desc") or "",
                         "start": xml_time(p.get("start")).isoformat(),
                         "stop": xml_time(p.get("stop")).isoformat()})
    return {"station": station, "programmes": schedule}


def validate_url(value):
    url = str(value or "").strip()
    parts = urlsplit(url)
    if (len(url) > 2048 or parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError("Use an HTTP or HTTPS XMLTV file URL without credentials or query parameters")
    return url


def _stage(stream, staging, accounts=()):
    from xtream_epg import _BoundedReader, clean_programme
    from xtream_accounts import safe_value
    staging.executescript("""
        CREATE TABLE channels (guide_id TEXT PRIMARY KEY,names_json TEXT,programme_count INTEGER);
        CREATE TABLE programmes (guide_id TEXT,start_utc TEXT,stop_utc TEXT,programme_xml TEXT,
                                 PRIMARY KEY(guide_id,start_utc,stop_utc));
    """)
    parser = ET.iterparse(_BoundedReader(stream, MAX_BYTES), events=("start", "end"))
    _, root = next(parser)
    if root.tag != "tv":
        raise ValueError("Expected an XMLTV tv document")
    for event, element in parser:
        if event != "end" or element.tag not in {"channel", "programme"}:
            continue
        if element.tag == "channel":
            guide = str(element.get("id") or "").strip()
            names = [safe_value(str(n.text or "").strip()[:256], accounts) for n in element.findall("display-name")[:8]]
            names = list(dict.fromkeys(n for n in names if n))
            if guide and len(guide) <= 500 and safe_value(guide, accounts) == guide and names:
                staging.execute("INSERT OR REPLACE INTO channels VALUES (?,?,0)", (guide, json.dumps(names)))
        else:
            guide = str(element.get("channel") or "")
            cleaned = clean_programme(element, {"effective_guide_id": guide}, accounts, "UTC")
            if cleaned is not None:
                staging.execute("INSERT OR REPLACE INTO programmes VALUES (?,?,?,?)",
                                (guide, cleaned.get("start"), cleaned.get("stop"), ET.tostring(cleaned, encoding="unicode")))
        root.remove(element)
    if not staging.execute("SELECT 1 FROM channels LIMIT 1").fetchone():
        raise ValueError("XMLTV has no usable channel definitions")
    staging.execute("DELETE FROM programmes WHERE guide_id NOT IN (SELECT guide_id FROM channels)")
    staging.execute("UPDATE channels SET programme_count=(SELECT COUNT(*) FROM programmes p WHERE p.guide_id=channels.guide_id)")
    staging.commit()


def refresh(conn, url=None, accounts=()):
    previous = status(conn)
    url = validate_url(url or previous.get("url"))
    # Importing a different lineup must not silently reuse its numeric IDs for
    # channels already assigned from the previous lineup.
    if previous and url != previous["url"] and conn.execute("SELECT 1 FROM sqlite_master WHERE name='xtream_persistent_channels'").fetchone():
        selected = conn.execute("SELECT 1 FROM xtream_persistent_channels WHERE epg_source_id LIKE 'xmltv:%' LIMIT 1").fetchone()
        if selected:
            raise ValueError("Clear the selected external guide links before changing the XMLTV source URL")
    try:
        with tempfile.TemporaryDirectory(prefix="fruit-xmltv-") as directory:
            staging = sqlite3.connect(directory + "/snapshot.db")
            try:
                deadline = time.monotonic() + 180
                with requests.get(url, stream=True, timeout=(10, 30)) as response:
                    response.raise_for_status()
                    with open(directory + "/guide.xml", "wb+") as stream:
                        count = 0
                        for chunk in response.iter_content(65536):
                            count += len(chunk)
                            if count > MAX_BYTES or time.monotonic() > deadline:
                                raise ValueError("XMLTV download exceeded its size or time limit")
                            stream.write(chunk)
                        stream.seek(0)
                        _stage(stream, staging, accounts)
                channel_count = staging.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
                programme_count = staging.execute("SELECT COUNT(*) FROM programmes").fetchone()[0]
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                ensure_schema(conn)
                with conn:
                    conn.execute("DELETE FROM external_xmltv_channels")
                    conn.execute("DELETE FROM external_xmltv_programmes")
                    conn.executemany("INSERT INTO external_xmltv_channels VALUES (?,?,?)", staging.execute("SELECT * FROM channels"))
                    conn.executemany("INSERT INTO external_xmltv_programmes VALUES (?,?,?,?)", staging.execute("SELECT * FROM programmes"))
                    conn.execute("INSERT OR REPLACE INTO external_xmltv_state VALUES (1,?,?,?,?,?,NULL)",
                                 (url, now, now, channel_count, programme_count))
            finally:
                staging.close()
    except Exception:
        if previous:
            with conn:
                conn.execute("UPDATE external_xmltv_state SET checked_at=?,last_error=? WHERE id=1",
                             (datetime.now(timezone.utc).isoformat(timespec="seconds"), "XMLTV refresh failed; previous snapshot kept"))
        raise ValueError("External XMLTV unavailable, too large, or malformed; previous snapshot kept") from None
    return status(conn)


def refresh_if_due(conn, accounts=()):
    state = status(conn)
    if not state:
        return
    checked = datetime.fromisoformat(state["checked_at"])
    if datetime.now(timezone.utc) - checked >= timedelta(hours=6):
        try:
            refresh(conn, accounts=accounts)
        except ValueError:
            pass


def apply_selected(conn, accounts=(), *, persistent_id=None):
    """Copy selected external schedules into the existing export cache offline."""
    from server.services.xtream_persistent import list_channels
    from xtream_epg import clean_programme
    from xtream_pool_schema import ensure_schema as ensure_pool_schema
    ensure_pool_schema(conn)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    count = 0
    for channel in list_channels(conn, enabled_only=True):
        if persistent_id is not None and channel["id"] != persistent_id:
            continue
        source = channel.get("epg_source_id") or ""
        if not source.startswith(PREFIX):
            continue
        rows = [clean_programme(p, channel, accounts, "UTC") for p in programmes(conn, source)]
        rows = [p for p in rows if p is not None]
        error = None if rows else "Selected external guide has no current programmes; unexpired cache kept"
        with conn:
            if rows:
                conn.execute("DELETE FROM xtream_epg_programmes WHERE persistent_id=?", (channel["id"],))
                conn.executemany("INSERT OR REPLACE INTO xtream_epg_programmes VALUES (?,?,?,?,?,?)",
                                 ((channel["id"], channel["stream_id"], channel["effective_guide_id"], p.get("start"), p.get("stop"), ET.tostring(p, encoding="unicode")) for p in rows))
            conn.execute("INSERT INTO xtream_epg_status VALUES (?,?,?,?,?) ON CONFLICT(persistent_id) DO UPDATE SET "
                         "checked_at=excluded.checked_at,last_success=COALESCE(excluded.last_success,last_success),"
                         "programme_count=CASE WHEN excluded.last_error IS NULL THEN excluded.programme_count ELSE programme_count END,last_error=excluded.last_error",
                         (channel["id"], now, now if rows else None, len(rows), error))
        count += len(rows)
    return count
