#!/usr/bin/env python3
"""Generic favorite-team event/feed matching and ranking explanations.

This module contains no provider- or team-specific constants.  It only scores
metadata already attached to an event/playable against user-supplied terms.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterable, Mapping


TEAM_FEED_SCORE = 100
PREFERRED_TERM_SCORE = 70
TEAM_ROLE_SCORE = 40
NEUTRAL_FEED_SCORE = 10
OPPONENT_FEED_SCORE = -30
AVOID_TERM_SCORE = -50

_MATCHUP_RE = re.compile(r"\s+(vs\.?|versus|at|@)\s+|\s+[-\u2013\u2014]\s+", re.I)


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _contains_term(text: str, term: str) -> bool:
    """Match a complete normalized token/phrase, never a raw substring."""
    normalized_term = _normalize_text(term)
    # One- and two-character aliases create too many obvious false positives.
    if len(normalized_term.replace(" ", "")) < 3:
        return False
    return f" {normalized_term} " in f" {_normalize_text(text)} "


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        marker = _normalize_text(text)
        if text and marker and marker not in seen:
            seen.add(marker)
            result.append(text)
    return result


def normalize_favorite_teams(value: Any) -> list[dict[str, Any]]:
    """Return the supported, non-secret team preference shape.

    Invalid rows are ignored so a malformed saved value cannot break playback.
    The Settings API performs stricter top-level validation before persistence.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(value, list):
        return []

    teams: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        team = str(item.get("team") or item.get("name") or "").strip()
        if not team:
            continue
        teams.append({
            "team": team,
            "aliases": _string_list(item.get("aliases")),
            "preferred_terms": _string_list(
                item.get("preferred_terms", item.get("preferred_broadcaster_terms"))
            ),
            "avoid_terms": _string_list(
                item.get("avoid_terms", item.get("disfavored_terms"))
            ),
            "enabled": bool(item.get("enabled", True)),
        })
    return teams


def _flatten_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _flatten_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _flatten_strings(child)


def _metadata_text(record: Mapping[str, Any], json_fields: Iterable[str]) -> str:
    values: list[str] = []
    for key, value in record.items():
        if value is None:
            continue
        if key in json_fields and isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                pass
        values.extend(_flatten_strings(value))
    return " | ".join(values)


def _team_terms(team: Mapping[str, Any]) -> list[str]:
    return _string_list([team.get("team"), *_string_list(team.get("aliases"))])


def _split_matchup(title: str) -> tuple[list[str], str | None]:
    match = _MATCHUP_RE.search(str(title or ""))
    if not match:
        return [], None
    left = str(title)[:match.start()].strip(" -|:")
    right = str(title)[match.end():].strip(" -|:")
    if not left or not right:
        return [], None
    separator = (match.group(1) or "-").lower().rstrip(".")
    return [left, right], separator


def find_favorite_teams(event: Mapping[str, Any], favorite_teams: Any) -> list[dict[str, Any]]:
    """Find enabled configured teams involved in an event."""
    teams = [team for team in normalize_favorite_teams(favorite_teams) if team["enabled"]]
    if not teams:
        return []
    event_fields = {
        key: value for key, value in event.items()
        if key in {
            "title", "title_brief", "synopsis", "synopsis_brief", "channel_name",
            "classification_json", "genres_json", "content_segments_json",
            "raw_attributes_json", "sport", "league", "home_team", "away_team",
            "team_names", "teams",
        }
        or "team" in key.casefold()
    }
    event_text = _metadata_text(
        event_fields,
        ("classification_json", "genres_json", "content_segments_json", "raw_attributes_json"),
    )
    return [
        team for team in teams
        if any(_contains_term(event_text, term) for term in _team_terms(team))
    ]


def _participant_terms(participant: str) -> list[str]:
    terms = [participant]
    tokens = _normalize_text(participant).split()
    if tokens and len(tokens[-1]) >= 4:
        terms.append(tokens[-1])
    return _string_list(terms)


def score_team_affinity(
    event: Mapping[str, Any],
    playable: Mapping[str, Any],
    favorite_teams: Any,
) -> dict[str, Any]:
    """Score one playable for an event and return an inspector-friendly explanation."""
    matched_teams = find_favorite_teams(event, favorite_teams)
    result: dict[str, Any] = {
        "score": 0,
        "matched_teams": [team["team"] for team in matched_teams],
        "reasons": [],
    }
    if not matched_teams:
        return result

    playable_fields = {
        key: playable.get(key) for key in (
            "service_name", "logical_service", "provider", "title", "feed_name",
            "feed_type", "stream_metadata_json", "category_name", "subcategory_name",
            "network",
        ) if playable.get(key) is not None
    }
    playable_text = _metadata_text(
        playable_fields,
        ("stream_metadata_json",),
    )
    title = str(event.get("title") or event.get("title_brief") or "")
    participants, separator = _split_matchup(title)

    favorite_participant_indexes: set[int] = set()
    for index, participant in enumerate(participants):
        if any(
            _contains_term(participant, term)
            for team in matched_teams
            for term in _team_terms(team)
        ):
            favorite_participant_indexes.add(index)

    participant_hits = {
        index for index, participant in enumerate(participants)
        if any(_contains_term(playable_text, term) for term in _participant_terms(participant))
    }
    favorite_feed_hits = participant_hits & favorite_participant_indexes
    opponent_feed_hits = participant_hits - favorite_participant_indexes

    # Events without a parseable matchup still support exact team/alias feeds,
    # which is important for team-channel Xtream entries.
    named_favorite_match = any(
        _contains_term(playable_text, term)
        for team in matched_teams
        for term in _team_terms(team)
    )
    if (favorite_feed_hits and not opponent_feed_hits) or (
        not participants and named_favorite_match
    ):
        result["score"] += TEAM_FEED_SCORE
        result["reasons"].append({
            "score": TEAM_FEED_SCORE,
            "reason": "favorite team feed",
        })
    elif opponent_feed_hits and not favorite_feed_hits:
        result["score"] += OPPONENT_FEED_SCORE
        result["reasons"].append({
            "score": OPPONENT_FEED_SCORE,
            "reason": "opponent-specific feed",
        })

    preferred_match = next((
        term
        for team in matched_teams
        for term in team.get("preferred_terms", [])
        if _contains_term(playable_text, term)
    ), None)
    if preferred_match:
        result["score"] += PREFERRED_TERM_SCORE
        result["reasons"].append({
            "score": PREFERRED_TERM_SCORE,
            "reason": f'preferred broadcaster/feed "{preferred_match}"',
        })

    feed_type = _normalize_text(playable.get("feed_type"))
    if (
        feed_type in ("home", "away")
        and separator in ("at", "@")
        and participants
        and favorite_participant_indexes
    ):
        # "A at B" provides an unambiguous away/home relationship. A bare
        # "vs" title does not, so named feed metadata decides that case.
        favorite_roles = {
            "away" if index == 0 else "home" for index in favorite_participant_indexes
        }
        if feed_type in favorite_roles:
            result["score"] += TEAM_ROLE_SCORE
            result["reasons"].append({
                "score": TEAM_ROLE_SCORE,
                "reason": f"favorite team {feed_type} feed",
            })
        else:
            result["score"] += OPPONENT_FEED_SCORE
            result["reasons"].append({
                "score": OPPONENT_FEED_SCORE,
                "reason": f"opponent {feed_type} feed",
            })
    elif feed_type in ("national", "neutral"):
        result["score"] += NEUTRAL_FEED_SCORE
        result["reasons"].append({
            "score": NEUTRAL_FEED_SCORE,
            "reason": f"{feed_type} feed",
        })

    avoid_match = next((
        term
        for team in matched_teams
        for term in team.get("avoid_terms", [])
        if _contains_term(playable_text, term)
    ), None)
    if avoid_match:
        result["score"] += AVOID_TERM_SCORE
        result["reasons"].append({
            "score": AVOID_TERM_SCORE,
            "reason": f'explicit avoid term "{avoid_match}"',
        })

    return result
