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
import subprocess
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
_PLACEHOLDER_LABELS = {
    "NO EVENT",
    "NO EVENT STREAMING",
    "NO STREAM",
    "OFFLINE",
    "TBA",
}
_MOTORSPORT_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_MOTORSPORT_DATE_RE = re.compile(
    r"^(?:MON|TUE|WED|THU|FRI|SAT|SUN)\s+"
    r"(?P<day>\d{1,2})\s+"
    r"(?P<month>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+"
    r"(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)\s+"
    r"(?P<timezone>[A-Z]{2,5})(?:\s*\([^)]*\))?$",
    re.IGNORECASE,
)
_MOTORSPORT_SESSIONS = {
    "RACE": "Race",
    "SPRINT": "Sprint",
    "QUALIFY": "Qualifying",
    "QUALIFYING": "Qualifying",
    "PRACTICE": "Practice",
    "PRACTICE 1": "Practice 1",
    "PRACTICE 2": "Practice 2",
    "PRACTICE 3": "Practice 3",
    "FP1": "Practice 1",
    "FP2": "Practice 2",
    "FP3": "Practice 3",
    "SPRINT QUALIFYING": "Sprint Qualifying",
    "SPRINT SHOOTOUT": "Sprint Shootout",
}
_MOTORSPORT_DURATION_MINUTES = {
    "Race": 240,
    "Sprint": 120,
    "Qualifying": 120,
    "Practice": 120,
    "Practice 1": 120,
    "Practice 2": 120,
    "Practice 3": 120,
    "Sprint Qualifying": 120,
    "Sprint Shootout": 120,
}
_SAFE_TIMEZONE_ABBREVIATIONS = {
    "EDT": timezone(timedelta(hours=-4), name="EDT"),
    "EST": timezone(timedelta(hours=-5), name="EST"),
    "UTC": timezone.utc,
    "GMT": timezone.utc,
}


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
    event_window_days: int = 7

    def validate(self, *, require_categories: bool = True) -> None:
        if not self.enabled:
            raise XtreamError("Xtream ingestion is disabled")
        if not self.server_url:
            raise XtreamError("XTREAM_SERVER_URL is required")
        if not self.username:
            raise XtreamError("XTREAM_USERNAME is required")
        if not self.password:
            raise XtreamError("XTREAM_PASSWORD is required")
        if require_categories and not self.category_ids:
            raise XtreamError(
                "At least one Xtream category ID is required; refusing to import the full catalogue"
            )
        if self.default_duration_minutes <= 0:
            raise XtreamError("XTREAM_DEFAULT_DURATION_MINUTES must be positive")
        if self.event_window_days <= 0:
            raise XtreamError("Xtream event import window must be positive")
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
    try:
        event_window_days = int(setting("days_ahead", "FRUIT_DAYS_AHEAD", 7))
    except (TypeError, ValueError) as exc:
        raise XtreamError("FRUIT_DAYS_AHEAD must be an integer") from exc

    enabled = _as_bool(setting("xtream_enabled", "XTREAM_ENABLED", False))

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
        event_window_days=event_window_days,
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
                 timeout: int = DEFAULT_TIMEOUT_SECONDS,
                 subprocess_runner=None, curl_binary: str = "curl"):
        # Category discovery is intentionally allowed before any categories have
        # been selected. Ingestion and tune-time URL construction still call the
        # normal validation path, which refuses an accidental full catalogue import.
        config.validate(require_categories=False)
        self.config = config
        self.session = session or requests.Session()
        self.timeout = timeout
        self.subprocess_runner = subprocess_runner or subprocess.run
        self.curl_binary = curl_binary

    @staticmethod
    def _usable_payload(payload: Any, required_key: str) -> Optional[list[dict]]:
        if not isinstance(payload, list):
            return None
        rows = [
            item for item in payload
            if isinstance(item, dict) and item.get(required_key) is not None
        ]
        # An empty list is structurally valid for curl (the final transport).
        # A non-empty list with no usable objects is never safe for stale
        # reconciliation.
        if payload and not rows:
            return None
        return rows

    def _get_with_requests(self, action: str,
                           category_id: Optional[str]) -> Optional[list[dict]]:
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
            required_key = "category_id" if action == "get_live_categories" else "stream_id"
            rows = self._usable_payload(response.json(), required_key)
            # Some incompatible providers return a misleading empty list to
            # Python clients. Give curl one chance; curl may confirm the empty
            # list as the final authoritative response.
            return rows if rows else None
        except Exception:
            # requests exceptions can embed the fully-authenticated request URL.
            # Do not propagate or log their text; curl gets one clean retry.
            return None

    def _get_with_curl(self, action: str,
                       category_id: Optional[str]) -> list[dict]:
        command = [
            self.curl_binary,
            "-4",
            "-sS",
            "-L",
            "--max-time",
            str(self.timeout),
            "--get",
            f"{self.config.server_url}/player_api.php",
            "--data-urlencode",
            f"username={self.config.username}",
            "--data-urlencode",
            f"password={self.config.password}",
            "--data-urlencode",
            f"action={action}",
        ]
        if category_id is not None:
            command.extend(("--data-urlencode", f"category_id={category_id}"))
        try:
            completed = self.subprocess_runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout + 5,
                check=False,
            )
        except Exception:
            # File/process errors may include command arguments. Never surface
            # the original exception because those arguments contain secrets.
            raise XtreamError(
                f"Xtream curl transport could not run for action {action}"
            ) from None
        if completed.returncode != 0:
            # curl stderr can include the requested URL, so only retain its
            # credential-free exit code.
            raise XtreamError(
                f"Xtream curl transport failed for action {action} "
                f"with exit code {completed.returncode}"
            )
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError):
            raise XtreamError(
                f"Xtream curl transport returned invalid JSON for action {action}"
            ) from None
        required_key = "category_id" if action == "get_live_categories" else "stream_id"
        rows = self._usable_payload(payload, required_key)
        if rows is None:
            raise XtreamError(
                f"Xtream curl transport returned an unusable response for action {action}"
            )
        return rows

    def _get(self, action: str, category_id: Optional[str] = None) -> list[dict]:
        rows = self._get_with_requests(action, category_id)
        if rows is not None:
            return rows
        return self._get_with_curl(action, category_id)

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


_NAME_SEPARATOR = r"[\sT|\-–—]+"
_NAME_TIME = (
    r"(?:"
    r"(?P<hour12>0?[1-9]|1[0-2])"
    r"(?::(?P<minute12>[0-5]\d)(?::(?P<second12>[0-5]\d))?)?"
    r"\s*(?P<ampm>AM|PM)"
    r"|"
    r"(?P<hour24>[01]?\d|2[0-3]):(?P<minute24>[0-5]\d)"
    r"(?::(?P<second24>[0-5]\d))?(?!\s*(?:AM|PM))"
    r")"
)
_NAME_TIME_PATTERNS = (
    (
        "iso",
        re.compile(
            rf"(?<!\d)(?P<year>\d{{4}})-(?P<month>\d{{1,2}})-(?P<day>\d{{1,2}})"
            rf"{_NAME_SEPARATOR}{_NAME_TIME}",
            re.IGNORECASE,
        ),
    ),
    (
        "us_with_year",
        re.compile(
            rf"(?<!\d)(?P<month>\d{{1,2}})/(?P<day>\d{{1,2}})/(?P<year>\d{{4}})"
            rf"{_NAME_SEPARATOR}{_NAME_TIME}",
            re.IGNORECASE,
        ),
    ),
    (
        "us_without_year",
        re.compile(
            rf"(?<![\d/])(?P<month>\d{{1,2}})/(?P<day>\d{{1,2}})"
            rf"{_NAME_SEPARATOR}{_NAME_TIME}",
            re.IGNORECASE,
        ),
    ),
    (
        "textual_month_without_year",
        re.compile(
            rf"(?<![A-Z])(?:MON|TUE|WED|THU|FRI|SAT|SUN)?\.?\s*"
            rf"(?P<month_name>JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|"
            rf"JUN(?:E)?|JUL(?:Y)?|AUG(?:UST)?|SEP(?:TEMBER)?|OCT(?:OBER)?|"
            rf"NOV(?:EMBER)?|DEC(?:EMBER)?)\s+(?P<day>\d{{1,2}})"
            rf"{_NAME_SEPARATOR}{_NAME_TIME}",
            re.IGNORECASE,
        ),
    ),
)

_STOP_TIMESTAMP_RE = re.compile(
    r"\bstop\s*:\s*(?P<value>\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)",
    re.IGNORECASE,
)


def _time_from_name_match(match: re.Match) -> tuple[int, int, int]:
    if match.group("hour12") is not None:
        hour = int(match.group("hour12"))
        minute = int(match.group("minute12") or 0)
        if match.group("ampm").upper() == "AM":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
        return hour, minute, int(match.group("second12") or 0)
    return (
        int(match.group("hour24")),
        int(match.group("minute24")),
        int(match.group("second24") or 0),
    )


def _reference_in_zone(now: Optional[datetime], zone: ZoneInfo) -> datetime:
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=zone)
    return reference.astimezone(zone)


def _infer_yearless_datetime(
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int,
    zone,
    now: Optional[datetime],
    event_window_days: int,
) -> Optional[datetime]:
    """Apply the shared previous/current/next-year window inference."""
    reference = _reference_in_zone(now, zone)
    candidates: list[datetime] = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(datetime(year, month, day, hour, minute, second, tzinfo=zone))
        except ValueError:
            continue
    if not candidates:
        return None
    closest = min(
        candidates,
        key=lambda candidate: (
            abs((candidate - reference).total_seconds()),
            0 if candidate >= reference else 1,
        ),
    )
    if abs(closest - reference) > timedelta(days=max(1, event_window_days)):
        return None
    return closest.astimezone(timezone.utc)


def _is_within_event_window(
    value: datetime, now: Optional[datetime], event_window_days: int
) -> bool:
    """Use one bounded policy for provider timestamps and name-derived dates."""
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return abs(value - reference) <= timedelta(days=max(1, event_window_days))


def parse_stop_from_name(name: Any, timezone_name: str = "UTC") -> Optional[datetime]:
    """Read an explicit provider ``stop:`` timestamp embedded in a stream name."""
    match = _STOP_TIMESTAMP_RE.search(str(name or ""))
    return parse_timestamp(match.group("value"), timezone_name) if match else None


def is_placeholder_stream_name(name: Any) -> bool:
    """Conservatively match exact pipe-delimited placeholder labels."""
    for segment in str(name or "").split("|"):
        normalized = re.sub(r"^[\s\-–—_]+|[\s\-–—_]+$", "", segment).upper()
        normalized = " ".join(normalized.split())
        if normalized in _PLACEHOLDER_LABELS:
            return True
    return False


def normalize_motorsport_session(value: Any) -> Optional[str]:
    normalized = " ".join(str(value or "").strip().upper().split())
    return _MOTORSPORT_SESSIONS.get(normalized)


def identify_motorsport_series(category_name: Any, stream_name: Any) -> Optional[dict[str, str]]:
    """Identify only series supported by observed provider metadata."""
    text = f"{category_name or ''} {stream_name or ''}"
    if re.search(r"(?<![A-Z0-9])F\s*1(?![A-Z0-9])", text, re.IGNORECASE) or re.search(
        r"\bFORMULA\s+(?:1|ONE)\b", text, re.IGNORECASE
    ):
        return {"series": "Formula 1", "series_code": "F1"}
    return None


def _display_words(value: str) -> str:
    return " ".join(part.capitalize() for part in value.strip().split())


def parse_motorsport_stream(
    name: Any,
    category_name: Any,
    timezone_name: str = "UTC",
    now: Optional[datetime] = None,
    event_window_days: int = 7,
) -> Optional[dict[str, Any]]:
    """Parse the observed pipe-delimited F1 PPV event-name format.

    The session/date helpers are series-neutral so future observed formats can
    reuse them. For now, series identification intentionally supports only F1;
    no FloRacing/NASCAR/etc. syntax is guessed without real samples.
    """
    text = str(name or "").strip()
    if not text or is_placeholder_stream_name(text):
        return None
    series = identify_motorsport_series(category_name, text)
    if series is None:
        return None

    segments = [segment.strip() for segment in text.split("|") if segment.strip()]
    for index, segment in enumerate(segments):
        date_match = _MOTORSPORT_DATE_RE.fullmatch(segment)
        if not date_match or index == 0:
            continue
        event_segment = segments[index - 1]
        if ":" not in event_segment:
            continue
        location_raw, session_raw = event_segment.rsplit(":", 1)
        session = normalize_motorsport_session(session_raw)
        location = _display_words(location_raw)
        if not location or session is None:
            continue

        timezone_abbreviation = date_match.group("timezone").upper()
        zone = _SAFE_TIMEZONE_ABBREVIATIONS.get(timezone_abbreviation)
        timezone_source = "name_abbreviation"
        if zone is None:
            zone = _configured_zone(timezone_name)
            timezone_source = "configured_timezone_fallback"
        start = _infer_yearless_datetime(
            _MOTORSPORT_MONTHS[date_match.group("month").upper()],
            int(date_match.group("day")),
            int(date_match.group("hour")),
            int(date_match.group("minute")),
            0,
            zone,
            now,
            event_window_days,
        )
        if start is None:
            return None
        return {
            **series,
            "sport": "Motorsports",
            "location": location,
            "event_name": location,
            "session_type": session,
            "start": start,
            "timezone_abbreviation": timezone_abbreviation,
            "timezone_source": timezone_source,
            "display_title": f"{series['series_code']} - {location} - {session}",
        }
    return None


def parse_start_from_name(
    name: Any,
    timezone_name: str = "UTC",
    now: Optional[datetime] = None,
    event_window_days: int = 7,
) -> Optional[datetime]:
    """Parse a provider name only when it contains a calendar date and time.

    Names may use an explicit year or an ``M/D`` date. For yearless names,
    candidates in the previous, current, and next year are compared in the
    configured provider timezone. The closest candidate must fall within the
    configured event-import window, preventing a blind current-year choice at
    New Year. Completely date-free names remain unparseable.
    """
    text = str(name or "")
    zone = _configured_zone(timezone_name)
    reference = _reference_in_zone(now, zone)
    for date_kind, pattern in _NAME_TIME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        month = int(match.group("month")) if match.groupdict().get("month") else _MOTORSPORT_MONTHS[
            match.group("month_name")[:3].upper()
        ]
        day = int(match.group("day"))
        hour, minute, second = _time_from_name_match(match)

        if date_kind not in {"us_without_year", "textual_month_without_year"}:
            try:
                parsed = datetime(
                    int(match.group("year")), month, day, hour, minute, second, tzinfo=zone
                )
            except ValueError:
                return None
            parsed_utc = parsed.astimezone(timezone.utc)
            return parsed_utc if _is_within_event_window(parsed_utc, now, event_window_days) else None

        return _infer_yearless_datetime(
            month, day, hour, minute, second, zone, reference, event_window_days
        )
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
    if is_placeholder_stream_name(original_name):
        return None

    motorsport = parse_motorsport_stream(
        original_name,
        category_name,
        config.timezone_name,
        now=now,
        event_window_days=config.event_window_days,
    )

    start = _first_timestamp(
        stream,
        ("start_timestamp", "start_utc", "start_time", "event_start", "epg_start"),
        config.timezone_name,
    ) or (motorsport or {}).get("start") or parse_start_from_name(
        original_name,
        config.timezone_name,
        now=now,
        event_window_days=config.event_window_days,
    )
    if start is None or not _is_within_event_window(start, now, config.event_window_days):
        return None

    end = _first_timestamp(
        stream,
        ("end_timestamp", "end_utc", "end_time", "event_end", "epg_end"),
        config.timezone_name,
    ) or parse_stop_from_name(original_name, config.timezone_name)
    duration_inferred = end is None or end <= start
    if duration_inferred:
        inferred_minutes = (
            _MOTORSPORT_DURATION_MINUTES.get(motorsport["session_type"])
            if motorsport else None
        ) or config.default_duration_minutes
        end = start + timedelta(minutes=inferred_minutes)

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
        "timing_source": (
            "motorsport_session_default" if duration_inferred and motorsport
            else "default_duration" if duration_inferred else "provider"
        ),
    }
    if motorsport:
        metadata.update({
            "sport": "motorsports",
            "series": motorsport["series"],
            "series_code": motorsport["series_code"],
            "event_name": motorsport["event_name"],
            "location": motorsport["location"],
            "session_type": motorsport["session_type"],
            "timezone_abbreviation": motorsport["timezone_abbreviation"],
            "timezone_source": motorsport["timezone_source"],
        })
    display_title = motorsport["display_title"] if motorsport else original_name
    classifications = [
        {"type": "provider_category", "value": category_name or str(category_id)}
    ]
    genres = ["Sports"]
    if motorsport:
        classifications.extend([
            {"type": "sport", "value": motorsport["sport"]},
            {"type": "league", "value": motorsport["series"]},
            {"type": "motorsport_series", "value": motorsport["series"]},
            {"type": "event_location", "value": motorsport["location"]},
            {"type": "session_type", "value": motorsport["session_type"]},
        ])
        genres.append("Motorsports")
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    runtime_secs = int((end - start).total_seconds())
    return {
        "event": {
            "id": event_id,
            "pvid": event_id,
            "slug": f"xtream-{stream_id}",
            "title": display_title,
            "title_brief": display_title,
            "synopsis": (
                f"{motorsport['series']} {motorsport['location']} {motorsport['session_type']}."
                if motorsport else f"Xtream live event from {category_name or 'configured category'}."
            ),
            "synopsis_brief": display_title,
            "channel_name": category_name or "Xtream",
            "channel_provider_id": str(category_id),
            "airing_type": "live",
            "classification_json": json.dumps(classifications),
            "genres_json": json.dumps(genres),
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
            "title": display_title,
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
                   now: Optional[datetime] = None) -> dict[str, Any]:
    """Normalize and atomically upsert a fully-fetched Xtream snapshot."""
    ensure_schema(conn)
    selected = set(config.category_ids)
    category_names = {
        str(item.get("category_id")): str(item.get("category_name") or "Xtream")
        for item in categories
        if item.get("category_id") is not None and str(item.get("category_id")) in selected
    }
    observed_playable_ids: set[str] = set()
    normalized_playable_ids: set[str] = set()
    imported = skipped = skipped_placeholder = 0

    try:
        conn.execute("BEGIN")
        for category_id in config.category_ids:
            for stream in streams_by_category.get(category_id, []):
                stream_id = stream.get("stream_id")
                if stream_id is not None:
                    observed_playable_ids.add(stable_playable_id(category_id, stream_id))
                if is_placeholder_stream_name(stream.get("name")):
                    skipped_placeholder += 1
                    continue
                normalized = normalize_stream(
                    stream, category_id, category_names.get(category_id, "Xtream"), config, now
                )
                if normalized is None:
                    skipped += 1
                    continue
                _upsert(conn, "events", _EVENT_COLUMNS, normalized["event"], "id")
                _upsert(conn, "playables", _PLAYABLE_COLUMNS, normalized["playable"],
                        "event_id,playable_id")
                normalized_playable_ids.add(normalized["playable"]["playable_id"])
                imported += 1

        prior = conn.execute(
            "SELECT event_id, playable_id FROM playables WHERE provider = ?", (PROVIDER,)
        ).fetchall()
        stale_rows = [(event_id, playable_id) for event_id, playable_id in prior
                      if playable_id not in normalized_playable_ids]
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

    result: dict[str, Any] = {
        "observed_upstream": len(observed_playable_ids),
        "normalized": len(normalized_playable_ids),
        "imported": imported,
        "skipped_placeholder": skipped_placeholder,
        "skipped_unparseable": skipped,
        "stale_removed": len(stale_rows),
    }
    # Persistent rows use the same category snapshot but remain separate from
    # dynamic events/playables. A reconciliation problem must never delete a
    # saved channel or affect non-Xtream data.
    from server.services.xtream_persistent import reconcile_channels
    result.update(reconcile_channels(conn, streams_by_category, category_names))
    return result


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
        client_factory=XtreamClient) -> dict[str, Any]:
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
    for item in result.get("persistent_reconciliations", []):
        print(
            "Xtream persistent channel reconciled: "
            f"id={item['id']} stream_id={item['old_stream_id']}->{item['new_stream_id']}"
        )
    print(
        "Xtream ingest complete: "
        f"observed_upstream={result['observed_upstream']} "
        f"normalized={result['normalized']} "
        f"imported={result['imported']} "
        f"skipped_placeholder={result['skipped_placeholder']} "
        f"skipped_unparseable={result['skipped_unparseable']} "
        f"stale_removed={result['stale_removed']} "
        f"persistent_configured={result['persistent_configured']} "
        f"persistent_available={result['persistent_available']} "
        f"persistent_reconciled={result['persistent_reconciled']} "
        f"persistent_unavailable={result['persistent_unavailable']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
