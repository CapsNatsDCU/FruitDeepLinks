"""Operator-configured XMLTV sources, independent of IPTV credentials.

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
    # Upgrade the single-feed snapshot and its assignments in one transaction.
    columns = {r[1] for r in conn.execute("PRAGMA table_info(external_xmltv_state)")}
    if "name" in columns:
        return
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT external_xmltv_schema")
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(external_xmltv_state)")}
        legacy = bool(columns) and "name" not in columns
        if legacy:
            for table in ("state", "channels", "programmes"):
                conn.execute(f"ALTER TABLE external_xmltv_{table} RENAME TO external_xmltv_{table}_legacy")
        conn.execute("""CREATE TABLE IF NOT EXISTS external_xmltv_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL,
            refreshed_at TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL DEFAULT '',
            channel_count INTEGER NOT NULL DEFAULT 0, programme_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS external_xmltv_channels (
            source_id INTEGER NOT NULL, guide_id TEXT NOT NULL, names_json TEXT NOT NULL,
            programme_count INTEGER NOT NULL, PRIMARY KEY(source_id,guide_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS external_xmltv_programmes (
            source_id INTEGER NOT NULL, guide_id TEXT NOT NULL, start_utc TEXT NOT NULL,
            stop_utc TEXT NOT NULL, programme_xml TEXT NOT NULL,
            PRIMARY KEY(source_id,guide_id,start_utc,stop_utc))""")
        if legacy:
            conn.execute("INSERT INTO external_xmltv_state SELECT id,'Existing XMLTV / zap2xml',url,"
                         "refreshed_at,checked_at,channel_count,programme_count,last_error FROM external_xmltv_state_legacy")
            conn.execute("INSERT INTO external_xmltv_channels SELECT 1,* FROM external_xmltv_channels_legacy")
            conn.execute("INSERT INTO external_xmltv_programmes SELECT 1,* FROM external_xmltv_programmes_legacy")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='xtream_persistent_channels'").fetchone():
                conn.execute("UPDATE xtream_persistent_channels SET epg_source_id='xmltv:1:' || substr(epg_source_id,7) "
                             "WHERE epg_source_id LIKE 'xmltv:%'")
            for table in ("state", "channels", "programmes"):
                conn.execute(f"DROP TABLE external_xmltv_{table}_legacy")
        conn.execute("RELEASE external_xmltv_schema")
        if own_transaction:
            conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO external_xmltv_schema")
        conn.execute("RELEASE external_xmltv_schema")
        if own_transaction:
            conn.rollback()
        raise


def sources(conn):
    ensure_schema(conn)
    keys = ("id", "name", "url", "refreshed_at", "checked_at", "channel_count", "programme_count", "last_error")
    return [dict(zip(keys, row)) for row in conn.execute(
        "SELECT id,name,url,refreshed_at,checked_at,channel_count,programme_count,last_error "
        "FROM external_xmltv_state ORDER BY id")]


def status(conn, source_id=1):
    return next((s for s in sources(conn) if s["id"] == source_id), {})


def summary(conn):
    states = sources(conn)
    return {"refreshed_at": max((s["refreshed_at"] for s in states), default="") or None,
            "channel_count": sum(s["channel_count"] for s in states),
            "programme_count": sum(s["programme_count"] for s in states)}


def _has_assignments(conn, source_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='xtream_persistent_channels'").fetchone():
        return False
    prefix = f"{PREFIX}{source_id}:"
    return bool(conn.execute("SELECT 1 FROM xtream_persistent_channels WHERE substr(epg_source_id,1,?)=? LIMIT 1",
                             (len(prefix), prefix)).fetchone())


def save_source(conn, name, url, *, source_id=None):
    ensure_schema(conn)
    name = str(name or "").strip()
    if not name or len(name) > 120:
        raise ValueError("Enter a source name up to 120 characters")
    url = validate_url(url)
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        previous = status(conn, source_id) if source_id is not None else None
        if source_id is not None and not previous:
            raise KeyError("XMLTV source not found")
        if previous and previous["url"] != url and _has_assignments(conn, source_id):
            raise ValueError("Clear the selected external guide links before changing this source URL")
        if previous:
            conn.execute("UPDATE external_xmltv_state SET name=?,url=? WHERE id=?", (name,url,source_id))
            if previous["url"] != url:
                conn.execute("DELETE FROM external_xmltv_channels WHERE source_id=?", (source_id,))
                conn.execute("DELETE FROM external_xmltv_programmes WHERE source_id=?", (source_id,))
                conn.execute("UPDATE external_xmltv_state SET refreshed_at='',checked_at='',channel_count=0,programme_count=0,last_error=NULL WHERE id=?", (source_id,))
        else:
            source_id = conn.execute("INSERT INTO external_xmltv_state(name,url) VALUES (?,?)", (name,url)).lastrowid
    return status(conn, source_id)


def delete_source(conn, source_id):
    ensure_schema(conn)
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if not status(conn, source_id):
            raise KeyError("XMLTV source not found")
        if _has_assignments(conn, source_id):
            raise ValueError("Reassign or clear this source's channel guide links before removing it")
        conn.execute("DELETE FROM external_xmltv_channels WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM external_xmltv_programmes WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM external_xmltv_state WHERE id=?", (source_id,))


def _identity(source_id, raw_id):
    return f"{PREFIX}{source_id}:{raw_id}"


def _split_identity(guide_id):
    if not isinstance(guide_id, str) or not guide_id.startswith(PREFIX):
        return None, None
    number, separator, raw = guide_id[len(PREFIX):].partition(":")
    if not separator or not number.isdigit() or not raw:
        return None, None
    return int(number), raw


def entries(conn, source_id=None):
    ensure_schema(conn)
    return [{"guide_id": _identity(row[0], row[1]), "raw_guide_id": row[1],
             "source_id": row[0], "source_name": row[4], "display_names": json.loads(row[2]),
             "programme_count": row[3], "source": "External XMLTV · " + row[4]}
            for row in conn.execute("SELECT c.source_id,c.guide_id,c.names_json,c.programme_count,s.name "
                                    "FROM external_xmltv_channels c JOIN external_xmltv_state s ON s.id=c.source_id "
                                    "WHERE (? IS NULL OR c.source_id=?) ORDER BY c.source_id,c.guide_id", (source_id,source_id))]


def programmes(conn, guide_id):
    ensure_schema(conn)
    source_id, raw_id = _split_identity(guide_id)
    now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S +0000")
    return [ET.fromstring(row[0]) for row in conn.execute(
        "SELECT programme_xml FROM external_xmltv_programmes WHERE source_id=? AND guide_id=? AND stop_utc>? ORDER BY start_utc LIMIT 10000",
        (source_id, raw_id, now))]


def browse(conn, query="", *, offset=0, limit=50, source_id=None):
    """Browse only the imported snapshot and show its current channel assignments."""
    from server.services.xtream_persistent import list_channels
    query = query.casefold().strip()
    assigned = {}
    for channel in list_channels(conn):
        assigned.setdefault(channel.get("epg_source_id"), []).append({
            "id": channel["id"], "display_name": channel["display_name"],
            "channel_number": channel["channel_number"], "enabled": channel["enabled"]})
    stations = [{**station, "assigned_channels": assigned.get(station["guide_id"], [])}
                for station in entries(conn, source_id)
                if not query or query in station["guide_id"].casefold() or query in station["source_name"].casefold()
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
    source_id, raw_id = _split_identity(guide_id)
    rows = conn.execute("SELECT programme_xml FROM external_xmltv_programmes "
                        "WHERE source_id=? AND guide_id=? AND stop_utc>? ORDER BY start_utc LIMIT 20",
                        (source_id, raw_id, now))
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


def refresh(conn, url=None, accounts=(), *, source_id=None):
    ensure_schema(conn)
    if source_id is None:
        previous = status(conn)
        if previous:
            source_id = previous["id"]
        else:
            previous = save_source(conn, "XMLTV / zap2xml", url)
            source_id = previous["id"]
    else:
        previous = status(conn, source_id)
        if not previous:
            raise KeyError("XMLTV source not found")
    url = validate_url(url or previous["url"])
    if url != previous["url"] and _has_assignments(conn, source_id):
        raise ValueError("Clear the selected external guide links before changing this source URL")
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
                    if not conn.in_transaction:
                        conn.execute("BEGIN IMMEDIATE")
                    current = status(conn, source_id)
                    if not current or current["url"] != previous["url"]:
                        raise ValueError("XMLTV source changed during refresh")
                    conn.execute("DELETE FROM external_xmltv_channels WHERE source_id=?", (source_id,))
                    conn.execute("DELETE FROM external_xmltv_programmes WHERE source_id=?", (source_id,))
                    conn.executemany("INSERT INTO external_xmltv_channels VALUES (?,?,?,?)", ((source_id,*row) for row in staging.execute("SELECT * FROM channels")))
                    conn.executemany("INSERT INTO external_xmltv_programmes VALUES (?,?,?,?,?)", ((source_id,*row) for row in staging.execute("SELECT * FROM programmes")))
                    conn.execute("UPDATE external_xmltv_state SET url=?,refreshed_at=?,checked_at=?,channel_count=?,programme_count=?,last_error=NULL WHERE id=?",
                                 (url, now, now, channel_count, programme_count, source_id))
            finally:
                staging.close()
    except Exception:
        if previous:
            with conn:
                conn.execute("UPDATE external_xmltv_state SET checked_at=?,last_error=? WHERE id=?",
                             (datetime.now(timezone.utc).isoformat(timespec="seconds"), "XMLTV refresh failed; previous snapshot kept", source_id))
        raise ValueError("External XMLTV unavailable, too large, or malformed; previous snapshot kept") from None
    return status(conn, source_id)


def refresh_if_due(conn, accounts=()):
    for state in sources(conn):
        checked = datetime.fromisoformat(state["checked_at"]) if state["checked_at"] else None
        if checked is None or datetime.now(timezone.utc) - checked >= timedelta(hours=6):
            try:
                refresh(conn, accounts=accounts, source_id=state["id"])
            except (ValueError, KeyError):
                pass


def apply_selected(conn, accounts=(), *, persistent_id=None, source_id=None):
    """Copy selected external schedules into the existing export cache offline."""
    from server.services.xtream_persistent import list_channels
    from xtream_epg import clean_programme
    from xtream_pool_schema import ensure_schema as ensure_pool_schema
    ensure_schema(conn)
    ensure_pool_schema(conn)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    count = 0
    for channel in list_channels(conn, enabled_only=True):
        if channel.get("guide_mode") == "team":
            continue
        if persistent_id is not None and channel["id"] != persistent_id:
            continue
        source = channel.get("epg_source_id") or ""
        if source_id is not None and _split_identity(source)[0] != source_id:
            continue
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
