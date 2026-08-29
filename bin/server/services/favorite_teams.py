"""Favorite-team Settings persistence and user-facing workflows."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from db.preferences import save_settings
from team_preferences import (
    TeamPreferenceValidationError,
    normalize_favorite_teams,
    score_team_affinity,
    validate_favorite_teams,
    validate_team_preference,
)


_PREFIX = "setting:"
_ENABLED_KEY = f"{_PREFIX}prefer_favorite_team_broadcaster"
_TEAMS_KEY = f"{_PREFIX}favorite_teams"


class StoredFavoriteTeamsMalformed(RuntimeError):
    """Raised when a write could overwrite malformed saved configuration."""


def _load_raw(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_preferences'"
        ).fetchone()
        if not exists:
            return {}
        rows = conn.execute(
            "SELECT key, value FROM user_preferences WHERE key IN (?, ?)",
            (_ENABLED_KEY, _TEAMS_KEY),
        ).fetchall()
        return {str(row[0]): row[1] for row in rows}
    except sqlite3.Error:
        return {}


def load_favorite_team_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    """Load without writing, preserving malformed stored data until user action."""
    raw = _load_raw(conn)
    errors: list[str] = []

    enabled = False
    enabled_raw = raw.get(_ENABLED_KEY)
    if enabled_raw is not None:
        try:
            parsed_enabled = json.loads(enabled_raw)
            if not isinstance(parsed_enabled, bool):
                raise ValueError
            enabled = parsed_enabled
        except (TypeError, ValueError, json.JSONDecodeError):
            errors.append("The saved global favorite-team toggle is malformed.")

    teams: list[dict[str, Any]] = []
    teams_raw = raw.get(_TEAMS_KEY)
    if teams_raw is not None:
        try:
            parsed_teams = json.loads(teams_raw)
            if not isinstance(parsed_teams, list):
                raise ValueError
            teams = normalize_favorite_teams(parsed_teams)
        except (TypeError, ValueError, json.JSONDecodeError):
            errors.append(
                "The saved favorite-team list is malformed. Import valid settings or reset it to recover."
            )

    return {
        "enabled": enabled,
        "teams": teams,
        "malformed": bool(errors),
        "errors": errors,
        "has_saved_value": teams_raw is not None,
    }


def _require_recoverable(state: dict[str, Any]) -> None:
    if state.get("malformed"):
        raise StoredFavoriteTeamsMalformed("; ".join(state.get("errors") or []))


def set_global_enabled(conn: sqlite3.Connection, enabled: Any) -> dict[str, Any]:
    if not isinstance(enabled, bool):
        raise TeamPreferenceValidationError(["enabled must be true or false"])
    if not save_settings(conn, {"prefer_favorite_team_broadcaster": enabled}):
        raise RuntimeError("Could not save favorite-team toggle")
    return load_favorite_team_settings(conn)


def create_team(conn: sqlite3.Connection, value: Any) -> dict[str, Any]:
    state = load_favorite_team_settings(conn)
    _require_recoverable(state)
    team = validate_team_preference(value, include_display_name_alias=True)
    teams = validate_favorite_teams([*state["teams"], team])
    if not save_settings(conn, {"favorite_teams": teams}):
        raise RuntimeError("Could not save favorite team")
    return {"team": team, "settings": load_favorite_team_settings(conn)}


def update_team(conn: sqlite3.Connection, index: int, value: Any) -> dict[str, Any]:
    state = load_favorite_team_settings(conn)
    _require_recoverable(state)
    if index < 0 or index >= len(state["teams"]):
        raise IndexError("Favorite team not found")
    team = validate_team_preference(value)
    teams = list(state["teams"])
    teams[index] = team
    teams = validate_favorite_teams(teams)
    if not save_settings(conn, {"favorite_teams": teams}):
        raise RuntimeError("Could not update favorite team")
    return {"team": team, "settings": load_favorite_team_settings(conn)}


def set_team_enabled(
    conn: sqlite3.Connection, index: int, enabled: Any
) -> dict[str, Any]:
    if not isinstance(enabled, bool):
        raise TeamPreferenceValidationError(["enabled must be true or false"])
    state = load_favorite_team_settings(conn)
    _require_recoverable(state)
    if index < 0 or index >= len(state["teams"]):
        raise IndexError("Favorite team not found")
    teams = list(state["teams"])
    teams[index] = dict(teams[index], enabled=enabled)
    if not save_settings(conn, {"favorite_teams": teams}):
        raise RuntimeError("Could not update favorite team")
    return {"team": teams[index], "settings": load_favorite_team_settings(conn)}


def delete_team(conn: sqlite3.Connection, index: int) -> dict[str, Any]:
    state = load_favorite_team_settings(conn)
    _require_recoverable(state)
    if index < 0 or index >= len(state["teams"]):
        raise IndexError("Favorite team not found")
    removed = state["teams"][index]
    teams = [team for position, team in enumerate(state["teams"]) if position != index]
    if not save_settings(conn, {"favorite_teams": teams}):
        raise RuntimeError("Could not delete favorite team")
    return {"deleted": removed, "settings": load_favorite_team_settings(conn)}


def reset_favorite_team_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    if not save_settings(conn, {
        "prefer_favorite_team_broadcaster": False,
        "favorite_teams": [],
    }):
        raise RuntimeError("Could not reset favorite-team settings")
    return load_favorite_team_settings(conn)


def export_favorite_team_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    state = load_favorite_team_settings(conn)
    _require_recoverable(state)
    return {
        "schema_version": 1,
        "enabled": state["enabled"],
        "teams": state["teams"],
    }


def import_favorite_team_settings(
    conn: sqlite3.Connection, payload: Any
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TeamPreferenceValidationError(["import must be a JSON object"])
    schema_version = payload.get("schema_version", 1)
    if type(schema_version) is not int or schema_version != 1:
        raise TeamPreferenceValidationError(["schema_version must be 1"])
    enabled = payload.get("enabled", False)
    if not isinstance(enabled, bool):
        raise TeamPreferenceValidationError(["enabled must be true or false"])
    teams = validate_favorite_teams(payload.get("teams", []))
    if not save_settings(conn, {
        "prefer_favorite_team_broadcaster": enabled,
        "favorite_teams": teams,
    }):
        raise RuntimeError("Could not import favorite-team settings")
    return load_favorite_team_settings(conn)


def preview_team_match(conn: sqlite3.Connection, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TeamPreferenceValidationError(["preview must be a JSON object"])
    event_title = payload.get("event_title")
    feed_name = payload.get("feed_name")
    errors: list[str] = []
    if not isinstance(event_title, str) or not event_title.strip():
        errors.append("event_title cannot be blank")
    if not isinstance(feed_name, str) or not feed_name.strip():
        errors.append("feed_name cannot be blank")
    if errors:
        raise TeamPreferenceValidationError(errors)

    state = load_favorite_team_settings(conn)
    _require_recoverable(state)
    teams = state["teams"]
    index = payload.get("team_index")
    if index is not None:
        if not isinstance(index, int) or index < 0 or index >= len(teams):
            raise TeamPreferenceValidationError(["team_index is invalid"])
        teams = [teams[index]]

    result = score_team_affinity(
        {"title": event_title.strip()},
        {"title": feed_name.strip(), "feed_name": feed_name.strip()},
        teams,
    )
    return {
        "global_enabled": state["enabled"],
        "event_title": event_title.strip(),
        "feed_name": feed_name.strip(),
        "team_preference": result,
    }
