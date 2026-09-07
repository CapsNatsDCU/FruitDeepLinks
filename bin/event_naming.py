"""Shared event/programming-name normalization.

The provider title remains source data.  ``normalized_name`` is an optional
operator override; when it is empty, exporters derive a conservative matchup
name from structured team/league metadata and add an available broadcast/feed
label.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, Iterable, Optional, Tuple


_SEPARATOR_RE = re.compile(r"\s+(?:at|vs\.?|v|@)\s+", re.IGNORECASE)
_DATE_PREFIX_RE = re.compile(
    r"^(?:\d{1,2}\s*-\s*)?"
    r"(?:\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{4}-\d{2}-\d{2})"
    r"(?:\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?\s+",
    re.IGNORECASE,
)


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    return value.strip(" -|:") or None


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _name_from_team(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, dict):
        return None
    for key in ("name", "displayName", "display_name", "teamName", "team_name", "shortName", "abbr", "value"):
        result = _text(value.get(key))
        if result:
            return result
    for key in ("lon", "localizedNames", "names"):
        names = value.get(key)
        if isinstance(names, list):
            for item in names:
                result = _name_from_team(item)
                if result:
                    return result
    return None


def _role(value: Dict[str, Any]) -> Optional[str]:
    for key in ("homeAway", "home_away", "role"):
        role = _text(value.get(key))
        if role:
            role = role.lower()
            if role in ("home", "host"):
                return "home"
            if role in ("away", "road", "visitor"):
                return "away"
    for key in ("home", "isHome", "is_home"):
        if key in value:
            raw = value.get(key)
            if isinstance(raw, bool):
                return "home" if raw else "away"
            if str(raw).strip().lower() in ("1", "true", "yes", "home"):
                return "home"
            if str(raw).strip().lower() in ("0", "false", "no", "away", "road"):
                return "away"
    return None


def _team_pair_from_metadata(event: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], bool]:
    """Return (home, away, roles_are_explicit)."""
    roots = [event]
    raw = _json(event.get("raw_attributes_json"))
    if raw is not None:
        roots.append(raw)

    home = away = None
    ordered = []
    for root in roots:
        for item in _walk_dicts(root):
            if home is None:
                for key in ("home_team", "homeTeam", "home_team_name", "homeTeamName"):
                    home = _name_from_team(item.get(key))
                    if home:
                        break
            if away is None:
                for key in ("away_team", "awayTeam", "road_team", "roadTeam", "away_team_name", "awayTeamName"):
                    away = _name_from_team(item.get(key))
                    if away:
                        break

            for key in ("competitors", "teams", "tm"):
                candidates = item.get(key)
                if not isinstance(candidates, list):
                    continue
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    name = _name_from_team(candidate)
                    if not name:
                        continue
                    role = _role(candidate)
                    if role == "home" and home is None:
                        home = name
                    elif role == "away" and away is None:
                        away = name
                    elif name not in ordered:
                        ordered.append(name)

    if home and away:
        return home, away, True
    if len(ordered) >= 2:
        return ordered[0], ordered[1], False
    return home, away, bool(home or away)


def extract_team_names(event: Dict[str, Any]) -> list[str]:
    """Return distinct team names exposed by structured or title metadata."""
    roots = [event]
    raw = _json(event.get("raw_attributes_json"))
    if raw is not None:
        roots.append(raw)
    names: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        name = _name_from_team(value)
        if not name:
            return
        marker = re.sub(r"\s+", " ", name).casefold()
        if marker not in seen:
            seen.add(marker)
            names.append(name)

    for root in roots:
        for item in _walk_dicts(root):
            for key in ("home_team", "homeTeam", "home_team_name", "homeTeamName",
                        "away_team", "awayTeam", "road_team", "roadTeam",
                        "away_team_name", "awayTeamName"):
                add(item.get(key))
            for key in ("competitors", "teams", "tm"):
                candidates = item.get(key)
                if isinstance(candidates, list):
                    for candidate in candidates:
                        if isinstance(candidate, dict):
                            add(candidate)

    structured_names = names
    names = []
    seen = set()
    for title in (event.get("title"), event.get("title_brief")):
        left, right, _ = _title_matchup(title)
        add(left)
        add(right)
    # Prefer authoritative participant objects over abbreviated title text
    # (for example, "Washington Capitals" over "Capitals").
    return structured_names or names


def _classification_league(value: Any) -> Optional[str]:
    parsed = _json(value)
    if isinstance(parsed, dict):
        return _text(parsed.get("league") or parsed.get("league_name"))
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict) and str(item.get("type", "")).lower() == "league":
                result = _text(item.get("value") or item.get("name"))
                if result:
                    return result
    return None


def _label_from_value(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        for key in ("name", "displayName", "display_name", "title", "label", "value"):
            result = _text(value.get(key))
            if result:
                return result
    return None


def _broadcast_label(event: Dict[str, Any]) -> Optional[str]:
    """Find an explicit broadcast/network/feed label without guessing one."""
    roots = [event]
    raw = _json(event.get("raw_attributes_json"))
    if raw is not None:
        roots.append(raw)
    keys = (
        "broadcast_name", "broadcaster", "network_name", "network",
        "broadcast", "feed_name", "channel_name",
    )
    for root in roots:
        for item in _walk_dicts(root):
            for key in keys:
                label = _label_from_value(item.get(key))
                if label and label.lower() not in {"sports", "sports event", "xtream"}:
                    return label
    return None


def _title_matchup(title: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    title = _text(title)
    if not title:
        return None, None, None

    # Pipe-delimited Xtream/social labels often have a clean matchup segment.
    candidates = [part.strip() for part in title.split("|") if part.strip()]
    pipe_league = None
    if len(candidates) >= 2 and not _SEPARATOR_RE.search(candidates[0]):
        pipe_league = _text(candidates[0].strip("[] "))
    candidates.append(title)
    for candidate in candidates:
        candidate = _DATE_PREFIX_RE.sub("", candidate).strip()
        match = _SEPARATOR_RE.search(candidate)
        if not match:
            continue
        left = _text(candidate[:match.start()])
        right = _text(candidate[match.end():])
        if not left or not right or len(left) > 100 or len(right) > 100:
            continue
        league = None
        if ":" in left:
            prefix, left = left.split(":", 1)
            league = _text(prefix.strip("[] "))
        return left, right, league or pipe_league
    return None, None, None


def build_normalized_name(event: Dict[str, Any], broadcast_name: Optional[str] = None) -> Optional[str]:
    """Build ``[League] Team A @ Team B (Broadcast)`` when supported.

    Explicit home/away metadata is rendered in the conventional road-at-home
    order.  Untyped competitor/title order is preserved rather than guessed.
    """
    home, away, explicit_roles = _team_pair_from_metadata(event)
    title_left, title_right, title_league = _title_matchup(event.get("title") or event.get("title_brief"))
    if not home and title_left:
        home, away = title_left, title_right
        explicit_roles = False
    if not home or not away:
        return None

    league = (
        _text(event.get("league_name") or event.get("league"))
        or _classification_league(event.get("classification_json"))
        or title_league
    )
    matchup = f"{away} @ {home}" if explicit_roles else f"{home} @ {away}"
    title = f"[{league}] {matchup}" if league else matchup
    broadcast = _text(broadcast_name) or _broadcast_label(event)
    if broadcast and broadcast.lower() not in title.lower():
        title = f"{title} ({broadcast})"
    return title


def programming_name(event: Dict[str, Any], broadcast_name: Optional[str] = None) -> str:
    """Return the name used for guide/M3U/XMLTV programming."""
    override = _text(event.get("normalized_name"))
    if override:
        return override
    generated = build_normalized_name(event, broadcast_name)
    if generated:
        return generated
    title = _text(event.get("title")) or "Sports Event"
    broadcast = _text(broadcast_name) or _broadcast_label(event)
    if broadcast and broadcast.lower() not in title.lower():
        return f"{title} ({broadcast})"
    return title


def ensure_normalized_name_column(conn: sqlite3.Connection) -> bool:
    """Add the nullable operator override column if an events table exists."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()
    if not table:
        return False
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "normalized_name" in columns:
        return False
    conn.execute("ALTER TABLE events ADD COLUMN normalized_name TEXT")
    conn.commit()
    return True
