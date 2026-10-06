"""Provider-backed persistent-channel EPG cache, keyed by explicit identities.

Prefer provider XMLTV (preserves rich metadata); fall back to the existing
per-stream Xtream EPG API. Export reads this cache and never contacts providers.
"""
from __future__ import annotations

import base64
import copy
import re
import sqlite3
import requests
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from xtream_accounts import safe_value
from xtream_ingest import XtreamError, parse_timestamp
from xtream_pool_schema import ensure_schema


class _BoundedReader:
    def __init__(self, raw, limit=128 * 1024 * 1024):
        self.raw, self.remaining = raw, limit

    def read(self, size=-1):
        data = self.raw.read(min(size if size >= 0 else 65536, self.remaining + 1))
        self.remaining -= len(data)
        if self.remaining < 0:
            raise ValueError("Provider XMLTV exceeds the configured parser bound")
        return data


def xml_time(value, zone="UTC"):
    text = str(value or "")
    try:
        if re.fullmatch(r"\d+\.\d+", text):
            value = float(text)
        parsed = datetime.strptime(text, "%Y%m%d%H%M%S %z") if re.fullmatch(r"\d{14} [+-]\d{4}", text) else parse_timestamp(value, zone)
        return parsed.astimezone(timezone.utc) if parsed else None
    except (ValueError, OverflowError, OSError):
        return None


def text(value, *, encoded=True):
    value = str(value or "")
    # Xtream encodes titles/descriptions in base64. Require printable UTF-8;
    # ordinary words that happen to match the alphabet stay ordinary words.
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8") if encoded else ""
        if decoded and all(c.isprintable() or c in "\r\n\t" for c in decoded):
            value = decoded
    except (ValueError, UnicodeError):
        pass
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", value)


def api_programme(row, channel, zone):
    # A per-stream endpoint is independent evidence of the stream identity.
    # If the payload also supplies a channel identity, it must agree.
    explicit = row.get("epg_channel_id") or row.get("channel_id")
    source_id = channel.get("epg_channel_id") or channel.get("guide_id")
    if explicit and str(explicit) not in {str(source_id), str(channel["stream_id"])}:
        return None
    start = xml_time(row.get("start_timestamp") or row.get("start") or row.get("start_time"), zone)
    stop = xml_time(row.get("stop_timestamp") or row.get("end_timestamp") or row.get("end") or row.get("stop"), zone)
    title = text(row.get("title") or row.get("name"))
    if not start or not stop or stop <= start or not title:
        return None
    programme = ET.Element("programme", channel=channel["effective_guide_id"],
                           start=start.strftime("%Y%m%d%H%M%S +0000"), stop=stop.strftime("%Y%m%d%H%M%S +0000"))
    ET.SubElement(programme, "title").text = title
    for tag, keys in (("sub-title", ("subtitle", "sub_title")), ("desc", ("description", "desc")), ("category", ("category",))):
        value = next((row.get(key) for key in keys if row.get(key)), None)
        for item in (value if isinstance(value, list) else [value]):
            if item:
                ET.SubElement(programme, tag).text = text(item)
    if row.get("episode_num"):
        ET.SubElement(programme, "episode-num", system="onscreen").text = text(row["episode_num"])
    if str(row.get("is_new", "")).lower() in {"1", "true"}:
        ET.SubElement(programme, "new")
    if str(row.get("is_repeat", "")).lower() in {"1", "true"}:
        ET.SubElement(programme, "previously-shown")
    if row.get("icon"):
        ET.SubElement(programme, "icon", src=str(row["icon"]))
    return programme


def _provider_xmltv_one(client, wanted, config):
    """Parse one account's XMLTV response, retaining explicitly wanted IDs."""
    result = {guide: [] for guide in wanted}
    response = None
    from xtream_transport import configure_session
    session = client.session if config is client.config else configure_session(requests.Session())
    try:
        response = session.get(f"{config.server_url}/xmltv.php",
                                      params={"username": config.username, "password": config.password},
                                      stream=True, timeout=(10, 60))
        response.raise_for_status()
        response.raw.decode_content = True
        parser = ET.iterparse(_BoundedReader(response.raw), events=("start", "end"))
        _, root = next(parser)
        if root.tag != "tv":
            raise ValueError()
        for event, element in parser:
            if event == "end" and element.tag in {"programme", "channel"}:
                guide = element.get("channel")
                if element.tag == "programme" and guide in wanted:
                    start, stop = xml_time(element.get("start"), config.timezone_name), xml_time(element.get("stop"), config.timezone_name)
                    now = datetime.now(timezone.utc)
                    if (element.findtext("title") and start and stop and stop > start and stop >= now - timedelta(days=1)
                            and start <= now + timedelta(days=31) and len(result[guide]) < 10000):
                        result[guide].append(copy.deepcopy(element))
                root.remove(element)
        return result
    except Exception:
        raise XtreamError("Provider XMLTV unavailable or malformed") from None
    finally:
        if response is not None:
            response.close()
        if session is not client.session:
            session.close()


def provider_xmltv(client, wanted):
    """Use the next enabled account if XMLTV transport or parsing fails."""
    if not wanted:
        return {}
    configs = getattr(client, "metadata_configs", None)
    if not isinstance(configs, (tuple, list)):
        configs = (client.config,)
    for config in configs:
        try:
            result = _provider_xmltv_one(client, wanted, config)
            prefer = getattr(type(client), "_prefer_metadata_config", None)
            if callable(prefer):
                prefer(client, config)
            return result
        except XtreamError:
            continue
    raise XtreamError("Provider XMLTV unavailable or malformed for all enabled accounts")


def clean_programme(programme, channel, accounts, zone):
    start, stop = xml_time(programme.get("start"), zone), xml_time(programme.get("stop"), zone)
    if not start or not stop or stop <= start or not programme.findtext("title"):
        return None
    # Bound retained schedule, while preserving all source-supported metadata
    # inside useful programmes. No generated times or descriptions.
    now = datetime.now(timezone.utc)
    if stop < now - timedelta(days=1) or start > now + timedelta(days=31):
        return None
    programme.set("channel", channel["effective_guide_id"])
    programme.set("start", start.strftime("%Y%m%d%H%M%S +0000"))
    programme.set("stop", stop.strftime("%Y%m%d%H%M%S +0000"))
    for element in programme.iter():
        if element.text:
            element.text = safe_value(text(element.text, encoded=False), accounts)
        if element.tail:
            element.tail = safe_value(text(element.tail, encoded=False), accounts)
        for key, value in list(element.attrib.items()):
            element.set(key, safe_value(text(value, encoded=False), accounts))
    # XMLTV's programme children have a defined order (notably icon before
    # episode-num, previously-shown before new). Keep supported source fields.
    tags = ("title", "sub-title", "desc", "credits", "date", "category", "keyword", "language", "orig-language",
            "length", "icon", "url", "country", "episode-num", "video", "audio", "previously-shown", "premiere",
            "last-chance", "new", "subtitles", "rating", "star-rating", "review", "image")
    programme[:] = sorted((child for child in programme if child.tag in tags), key=lambda child: tags.index(child.tag))
    return programme


def refresh_epg(conn, client, accounts=()):
    from server.services.xtream_persistent import list_channels
    ensure_schema(conn)
    channels = list_channels(conn, enabled_only=True)
    if not channels:
        return {"channels": 0, "programmes": 0, "failed": 0}
    wanted = {str(c.get("epg_channel_id") or c.get("guide_id")) for c in channels if c.get("epg_channel_id") or c.get("guide_id")}
    try:
        xml = provider_xmltv(client, wanted)
    except XtreamError:
        xml = {}
    totals = {"channels": len(channels), "programmes": 0, "failed": 0}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for channel in channels:
        source_id = str(channel.get("epg_channel_id") or channel.get("guide_id") or "")
        programmes = xml.get(source_id, [])
        error = None
        if not programmes:
            try:
                get_epg = getattr(client, "get_epg", None) or client.get_short_epg
                rows = get_epg(channel["stream_id"])
                if not isinstance(rows, list):
                    raise ValueError()
                programmes = [p for row in rows if isinstance(row, dict)
                              if (p := api_programme(row, channel, client.config.timezone_name)) is not None]
                if rows and not programmes:
                    error = "EPG rows had no valid matching programme times/title"
            except Exception:
                error = "Provider EPG unavailable; retained unexpired cached programmes"
        cleaned = [p for programme in programmes
                   if (p := clean_programme(programme, channel, list(accounts), client.config.timezone_name)) is not None]
        with conn:
            if not error:
                conn.execute("DELETE FROM xtream_epg_programmes WHERE persistent_id=?", (channel["id"],))
                for p in cleaned:
                    conn.execute("INSERT OR REPLACE INTO xtream_epg_programmes VALUES(?,?,?,?,?,?)",
                                 (channel["id"], channel["stream_id"], channel["effective_guide_id"], p.get("start"), p.get("stop"), ET.tostring(p, encoding="unicode")))
            conn.execute("INSERT INTO xtream_epg_status(persistent_id,checked_at,last_success,programme_count,last_error) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(persistent_id) DO UPDATE SET checked_at=excluded.checked_at,last_success=COALESCE(excluded.last_success,last_success),"
                         "programme_count=CASE WHEN excluded.last_error IS NULL THEN excluded.programme_count ELSE programme_count END,last_error=excluded.last_error",
                         (channel["id"], now, None if error else now, len(cleaned), error))
        totals["programmes"] += len(cleaned)
        totals["failed"] += int(error is not None)
    with conn:
        conn.execute("DELETE FROM xtream_epg_programmes WHERE stop_utc<?", (datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S +0000"),))
        conn.execute("DELETE FROM xtream_epg_programmes WHERE persistent_id NOT IN (SELECT id FROM xtream_persistent_channels)")
        conn.execute("DELETE FROM xtream_epg_status WHERE persistent_id NOT IN (SELECT id FROM xtream_persistent_channels)")
    return totals


def cached_programmes(conn, channel):
    try:
        rows = conn.execute("SELECT programme_xml FROM xtream_epg_programmes WHERE persistent_id=? AND stream_id=? AND guide_id=? AND stop_utc>? ORDER BY start_utc",
                            (channel["id"], channel["stream_id"], channel["effective_guide_id"], datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S +0000")))
    except sqlite3.OperationalError:
        return []
    result = []
    for row in rows:
        try:
            result.append(ET.fromstring(row[0]))
        except ET.ParseError:
            continue
    return result
