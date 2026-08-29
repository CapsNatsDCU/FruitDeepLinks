#!/usr/bin/env python3
"""Ingest configured Xtream live streams into FruitDeepLinks.

Xtream credentials are used only for API requests and tune-time URL
construction.  They are never written to SQLite or included in log output.
Only streams with a reliable start time are imported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

try:
    from db.preferences import get_setting
except ImportError:
    def get_setting(conn, key, fallback=None):
        return fallback


PROVIDER = "xtream"
LOGICAL_SERVICE = "xtream"
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_DURATION_MINUTES = 180
_SAFE_EXTENSION_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")


class XtreamError(RuntimeError):
    """Safe-to-display Xtream failure with no credential-bearing URL."""


@dataclass(frozen=True)
class XtreamConfig:
    enabled: bool
    server_url: str
    username: str
    password: str
    category_ids: tuple[str, ...]
    timezone_name: str = "UTC"
    default_duration_minutes: int = DEFAULT_DURATION_MINUTES

    def validate(self) -> None:
        if not self.enabled:
            raise XtreamError("Xtream ingestion is disabled")
        if not self.server_url:
            raise XtreamError("XTREAM_SERVER_URL is required")
        if not self.username:
            raise XtreamError("XTREAM_USERNAME is required")
        if not self.password:
            raise XtreamError("XTREAM_PASSWORD is required")
        if not self.category_ids:
            raise XtreamError(
                "At least one Xtream category ID is required; refusing to import the full catalogue"
            )
        if self.default_duration_minutes <= 0:
            raise XtreamError("XTREAM_DEFAULT_DURATION_MINUTES must be positive")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise XtreamError(f"Unknown XTREAM_TIMEZONE: {self.timezone_name}") from exc


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def parse_category_ids(value: Any) -> tuple[str, ...]:
    """Parse JSON-list, comma-separated, or iterable category configuration."""
    if value is None:
        return ()
    # db.preferences treats a digit-only string setting as JSON and therefore
    # returns an int. A single category ID is still valid configuration.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [value]
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return ()
        if raw.startswith("["):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise XtreamError("XTREAM_CATEGORY_IDS contains invalid JSON") from exc
        else:
            value = raw.split(",")
    if not isinstance(value, (list, tuple, set)):
        raise XtreamError("XTREAM_CATEGORY_IDS must be a comma-separated string or JSON list")
    result: list[str] = []
    for item in value:
        category_id = str(item).strip()
        if category_id and category_id not in result:
            result.append(category_id)
    return tuple(result)


def load_config(conn: Optional[sqlite3.Connection] = None,
                environ: Optional[Mapping[str, str]] = None) -> XtreamConfig:
    env = os.environ if environ is None else environ

    def setting(key: str, env_key: str, default: Any) -> Any:
        if conn is not None:
            return get_setting(conn, key, default)
        return env.get(env_key, default)

    # Secrets intentionally have no DB/settings-UI fallback.
    username = env.get("XTREAM_USERNAME", "")
    password = env.get("XTREAM_PASSWORD", "")
    duration_raw = setting(
        "xtream_default_duration_minutes",
        "XTREAM_DEFAULT_DURATION_MINUTES",
        DEFAULT_DURATION_MINUTES,
    )
    try:
        duration = int(duration_raw)
    except (TypeError, ValueError) as exc:
        raise XtreamError("XTREAM_DEFAULT_DURATION_MINUTES must be an integer") from exc

    enabled = _as_bool(setting("xtream_enabled", "XTREAM_ENABLED", False))
    # Match the other scraper toggles: an explicit false environment value is
    # a hard operational override even if the Settings page previously saved
    # the scraper as enabled.
    if env.get("XTREAM_ENABLED", "").strip().lower() in ("0", "false", "no", "off"):
        enabled = False

    return XtreamConfig(
        enabled=enabled,
        server_url=str(setting("xtream_server_url", "XTREAM_SERVER_URL", "") or "").rstrip("/"),
        username=username,
        password=password,
        category_ids=parse_category_ids(
            setting("xtream_category_ids", "XTREAM_CATEGORY_IDS", "")
        ),
        timezone_name=str(setting("xtream_timezone", "XTREAM_TIMEZONE", "UTC") or "UTC"),
        default_duration_minutes=duration,
    )


def redact_credentials(text: Any, config: XtreamConfig) -> str:
    """Remove raw and URL-encoded credential values from diagnostic text."""
    redacted = str(text)
    for secret in (config.username, config.password):
        if not secret:
            continue
        for form in {secret, quote(secret, safe="")}:
            redacted = redacted.replace(form, "[REDACTED]")
    return redacted


def build_stream_url(config: XtreamConfig, stream_id: Any,
                     extension: Any = "ts") -> str:
    """Construct a standard Xtream live URL with encoded path segments."""
    config.validate()
    stream_id_text = str(stream_id).strip()
    if not stream_id_text:
        raise XtreamError("Xtream stream_id is required")
    extension_text = str(extension or "ts").strip().lstrip(".")
    if not _SAFE_EXTENSION_RE.fullmatch(extension_text):
        extension_text = "ts"
    return (
        f"{config.server_url}/live/{quote(config.username, safe='')}/"
        f"{quote(config.password, safe='')}/{quote(stream_id_text, safe='')}."
        f"{extension_text.lower()}"
    )


class XtreamClient:
    def __init__(self, config: XtreamConfig, session=None,
                 timeout: int = DEFAULT_TIMEOUT_SECONDS):
        config.validate()
        self.config = config
        self.session = session or requests.Session()
        self.timeout = timeout

    def _get(self, action: str, category_id: Optional[str] = None) -> list[dict]:
        params = {
            "username": self.config.username,
            "password": self.config.password,
            "action": action,
        }
        if category_id is not None:
            params["category_id"] = str(category_id)
        try:
            response = self.session.get(
                f"{self.config.server_url}/player_api.php",
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            # requests exceptions can embed the fully-authenticated request URL.
            raise XtreamError(f"Xtream API request failed for action {action}") from None
        if not isinstance(payload, list):
            raise XtreamError(f"Xtream API returned an unexpected response for action {action}")
        return [item for item in payload if isinstance(item, dict)]

    def get_live_categories(self) -> list[dict]:
        return self._get("get_live_categories")

    def get_live_streams(self, category_id: str) -> list[dict]:
        return self._get("get_live_streams", category_id=category_id)


def stable_event_id(category_id: Any, stream_id: Any) -> str:
    identity = f"{PROVIDER}\0{category_id}\0{stream_id}".encode("utf-8")
    return f"xtream:{hashlib.sha256(identity).hexdigest()[:24]}"


def stable_playable_id(category_id: Any, stream_id: Any) -> str:
    return f"{stable_event_id(category_id, stream_id)}:playable"


def _configured_zone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise XtreamError(f"Unknown XTREAM_TIMEZONE: {name}") from exc


def parse_timestamp(value: Any, timezone_name: str = "UTC") -> Optional[datetime]:
    """Parse explicit epoch/ISO timestamps without guessing a calendar date."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        if number > 10_000_000_000:  # milliseconds
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %I:%M %p"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_configured_zone(timezone_name))
    return parsed.astimezone(timezone.utc)


_NAME_TIME_PATTERNS = (
    re.compile(
        r"(?P<date>\d{4}-\d{1,2}-\d{1,2})[ T|\-]+"
        r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<date>\d{1,2}/\d{1,2}/\d{4})[ T|\-]+"
        r"(?P<time>\d{1,2}:\d{2}\s*(?:AM|PM)?)",
        re.IGNORECASE,
    ),
)


def parse_start_from_name(name: Any, timezone_name: str = "UTC") -> Optional[datetime]:
    """Parse only names containing a complete date plus time.

    Time-only names such as ``NHL | Team A @ Team B | 7:00 PM`` are rejected;
    assigning today's date would create incorrect guide entries around time-zone
    and midnight boundaries.
    """
    text = str(name or "")
    for pattern in _NAME_TIME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        candidate = f"{match.group('date')} {match.group('time')}"
        for fmt in (
            "%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
            "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M",
        ):
            try:
                parsed = datetime.strptime(candidate.upper(), fmt)
                return parsed.replace(tzinfo=_configured_zone(timezone_name)).astimezone(timezone.utc)
            except ValueError:
                continue
    return None


def _first_timestamp(stream: Mapping[str, Any], keys: Iterable[str],
                     timezone_name: str) -> Optional[datetime]:
    for key in keys:
        parsed = parse_timestamp(stream.get(key), timezone_name)
        if parsed is not None:
            return parsed
    return None


def normalize_stream(stream: Mapping[str, Any], category_id: str,
                     category_name: str, config: XtreamConfig,
                     now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    stream_id = stream.get("stream_id")
    original_name = str(stream.get("name") or "").strip()
    if stream_id is None or not original_name:
        return None

    start = _first_timestamp(
        stream,
        ("start_timestamp", "start_utc", "start_time", "event_start", "epg_start"),
        config.timezone_name,
    ) or parse_start_from_name(original_name, config.timezone_name)
    if start is None:
        return None

    end = _first_timestamp(
        stream,
        ("end_timestamp", "end_utc", "end_time", "event_end", "epg_end"),
        config.timezone_name,
    )
    duration_inferred = end is None or end <= start
    if duration_inferred:
        end = start + timedelta(minutes=config.default_duration_minutes)

    seen = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    event_id = stable_event_id(category_id, stream_id)
    extension = str(stream.get("container_extension") or "ts").lstrip(".")
    if not _SAFE_EXTENSION_RE.fullmatch(extension):
        extension = "ts"
    icon = stream.get("stream_icon") or None
    epg_channel_id = stream.get("epg_channel_id") or stream.get("epg_id") or None
    metadata = {
        "provider": PROVIDER,
        "stream_id": str(stream_id),
        "category_id": str(category_id),
        "category_name": category_name,
        "original_stream_name": original_name,
        "stream_icon": icon,
        "epg_channel_id": epg_channel_id,
        "container_extension": extension,
        "duration_inferred": duration_inferred,
        "timing_source": "default_duration" if duration_inferred else "provider",
    }
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    runtime_secs = int((end - start).total_seconds())
    return {
        "event": {
            "id": event_id,
            "pvid": event_id,
            "slug": f"xtream-{stream_id}",
            "title": original_name,
            "title_brief": original_name,
            "synopsis": f"Xtream live event from {category_name or 'configured category'}.",
            "synopsis_brief": original_name,
            "channel_name": category_name or "Xtream",
            "channel_provider_id": str(category_id),
            "airing_type": "live",
            "classification_json": json.dumps([
                {"type": "provider_category", "value": category_name or str(category_id)}
            ]),
            "genres_json": json.dumps(["Sports"]),
            "content_segments_json": "[]",
            "is_free": 0,
            "is_premium": 1,
            "runtime_secs": runtime_secs,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start_utc": start.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "end_utc": end.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "created_ms": int(seen.timestamp() * 1000),
            "created_utc": seen.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "hero_image_url": icon,
            "last_seen_utc": seen.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "raw_attributes_json": json.dumps(metadata, separators=(",", ":")),
        },
        "playable": {
            "event_id": event_id,
            "playable_id": stable_playable_id(category_id, stream_id),
            "provider": PROVIDER,
            "service_name": "Xtream IPTV",
            "logical_service": LOGICAL_SERVICE,
            "deeplink_play": None,
            "deeplink_open": None,
            "http_deeplink_url": None,
            "playable_url": None,
            "stream_url": None,
            "stream_id": str(stream_id),
            "stream_extension": extension,
            "stream_metadata_json": json.dumps(metadata, separators=(",", ":")),
            "title": original_name,
            "content_id": str(epg_channel_id or stream_id),
            "priority": 24,
            "created_utc": seen.isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
    }


def ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, pvid TEXT, slug TEXT, title TEXT, title_brief TEXT,
        synopsis TEXT, synopsis_brief TEXT, channel_name TEXT, channel_provider_id TEXT,
        airing_type TEXT, classification_json TEXT, genres_json TEXT, content_segments_json TEXT,
        is_free INTEGER, is_premium INTEGER, runtime_secs INTEGER, start_ms INTEGER, end_ms INTEGER,
        start_utc TEXT, end_utc TEXT, created_ms INTEGER, created_utc TEXT,
        hero_image_url TEXT, last_seen_utc TEXT, raw_attributes_json TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS playables (
        event_id TEXT NOT NULL, playable_id TEXT NOT NULL, provider TEXT,
        service_name TEXT, logical_service TEXT, deeplink_play TEXT, deeplink_open TEXT,
        http_deeplink_url TEXT, playable_url TEXT, title TEXT, content_id TEXT,
        priority INTEGER DEFAULT 0, created_utc TEXT,
        PRIMARY KEY (event_id, playable_id),
        FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS event_images (
        event_id TEXT, img_type TEXT, url TEXT,
        PRIMARY KEY (event_id, img_type, url))""")

    existing = {row[1] for row in cur.execute("PRAGMA table_info(playables)")}
    additions = {
        "service_name": "TEXT", "logical_service": "TEXT",
        "http_deeplink_url": "TEXT", "espn_graph_id": "TEXT", "locale": "TEXT",
        "stream_url": "TEXT", "stream_id": "TEXT", "stream_extension": "TEXT",
        "stream_metadata_json": "TEXT",
    }
    for column, declaration in additions.items():
        if column not in existing:
            cur.execute(f"ALTER TABLE playables ADD COLUMN {column} {declaration}")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_playables_stream_id ON playables(provider, stream_id)")
    conn.commit()


_EVENT_COLUMNS = (
    "id", "pvid", "slug", "title", "title_brief", "synopsis", "synopsis_brief",
    "channel_name", "channel_provider_id", "airing_type", "classification_json",
    "genres_json", "content_segments_json", "is_free", "is_premium", "runtime_secs",
    "start_ms", "end_ms", "start_utc", "end_utc", "created_ms", "created_utc",
    "hero_image_url", "last_seen_utc", "raw_attributes_json",
)
_PLAYABLE_COLUMNS = (
    "event_id", "playable_id", "provider", "service_name", "logical_service",
    "deeplink_play", "deeplink_open", "http_deeplink_url", "playable_url",
    "stream_url", "stream_id", "stream_extension", "stream_metadata_json",
    "title", "content_id", "priority", "created_utc",
)


def _upsert(conn: sqlite3.Connection, table: str, columns: tuple[str, ...],
            values: Mapping[str, Any], conflict: str) -> None:
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{column}=excluded.{column}" for column in columns if column not in conflict.split(","))
    conn.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict}) DO UPDATE SET {updates}",
        tuple(values.get(column) for column in columns),
    )


def ingest_payload(conn: sqlite3.Connection, categories: list[dict],
                   streams_by_category: Mapping[str, list[dict]], config: XtreamConfig,
                   now: Optional[datetime] = None) -> dict[str, int]:
    """Normalize and atomically upsert a fully-fetched Xtream snapshot."""
    ensure_schema(conn)
    selected = set(config.category_ids)
    category_names = {
        str(item.get("category_id")): str(item.get("category_name") or "Xtream")
        for item in categories
        if item.get("category_id") is not None and str(item.get("category_id")) in selected
    }
    fetched_playable_ids: set[str] = set()
    imported = skipped = 0

    try:
        conn.execute("BEGIN")
        for category_id in config.category_ids:
            for stream in streams_by_category.get(category_id, []):
                stream_id = stream.get("stream_id")
                if stream_id is not None:
                    fetched_playable_ids.add(stable_playable_id(category_id, stream_id))
                normalized = normalize_stream(
                    stream, category_id, category_names.get(category_id, "Xtream"), config, now
                )
                if normalized is None:
                    skipped += 1
                    continue
                _upsert(conn, "events", _EVENT_COLUMNS, normalized["event"], "id")
                _upsert(conn, "playables", _PLAYABLE_COLUMNS, normalized["playable"],
                        "event_id,playable_id")
                imported += 1

        prior = conn.execute(
            "SELECT event_id, playable_id FROM playables WHERE provider = ?", (PROVIDER,)
        ).fetchall()
        stale_rows = [(event_id, playable_id) for event_id, playable_id in prior
                      if playable_id not in fetched_playable_ids]
        for event_id, playable_id in stale_rows:
            conn.execute(
                "DELETE FROM playables WHERE event_id = ? AND playable_id = ? AND provider = ?",
                (event_id, playable_id, PROVIDER),
            )
        for event_id in {row[0] for row in stale_rows}:
            remaining = conn.execute(
                "SELECT 1 FROM playables WHERE event_id = ? LIMIT 1", (event_id,)
            ).fetchone()
            raw_row = conn.execute(
                "SELECT raw_attributes_json FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if remaining or not raw_row:
                continue
            try:
                owned = json.loads(raw_row[0] or "{}").get("provider") == PROVIDER
            except (TypeError, json.JSONDecodeError):
                owned = False
            if owned:
                conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"imported": imported, "skipped_unparseable": skipped, "stale_removed": len(stale_rows)}


def fetch_snapshot(client: XtreamClient, config: XtreamConfig) -> tuple[list[dict], dict[str, list[dict]]]:
    categories = client.get_live_categories()
    available = {str(item.get("category_id")) for item in categories}
    unknown = [category_id for category_id in config.category_ids if category_id not in available]
    if unknown:
        raise XtreamError(f"Configured Xtream category IDs were not returned by the provider: {', '.join(unknown)}")
    streams = {category_id: client.get_live_streams(category_id)
               for category_id in config.category_ids}
    return categories, streams


def run(db_path: Path, environ: Optional[Mapping[str, str]] = None,
        client_factory=XtreamClient) -> dict[str, int]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        config = load_config(conn, environ)
        config.validate()
        categories, streams = fetch_snapshot(client_factory(config), config)
        return ingest_payload(conn, categories, streams, config)
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ingest configured Xtream live categories")
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = run(args.db)
    except XtreamError as exc:
        # XtreamError messages are deliberately credential-free.
        print(f"Xtream ingest failed: {exc}")
        return 1
    print(
        "Xtream ingest complete: "
        f"imported={result['imported']} "
        f"skipped_unparseable={result['skipped_unparseable']} "
        f"stale_removed={result['stale_removed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
