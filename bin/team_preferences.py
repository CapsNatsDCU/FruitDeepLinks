#!/usr/bin/env python3
"""Conservative favorite-team identity matching and playable ranking."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterable, Mapping

TEAM_FEED_SCORE, PREFERRED_TERM_SCORE, TEAM_ROLE_SCORE = 100, 70, 40
NEUTRAL_FEED_SCORE, OPPONENT_FEED_SCORE, AVOID_TERM_SCORE = 10, -30, -50
_MATCHUP_RE = re.compile(r"\s+(vs\.?|versus|at|@)\s+|\s+[-\u2013\u2014]\s+", re.I)


class TeamPreferenceValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _contains_term(text: str, term: str) -> bool:
    term = _normalize_text(term)
    return len(term.replace(" ", "")) >= 3 and f" {term} " in f" {_normalize_text(text)} "


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str): value = [value]
    if not isinstance(value, (list, tuple)): return []
    result, seen = [], set()
    for item in value:
        text, marker = str(item or "").strip(), _normalize_text(item)
        if text and marker and marker not in seen: seen.add(marker); result.append(text)
    return result


def _strict_string_list(value: Any, field: str) -> list[str]:
    if value is None: return []
    if not isinstance(value, (list, tuple)): raise TeamPreferenceValidationError([f"{field} must be an array of strings"])
    errors, result, seen = [], [], set()
    for index, item in enumerate(value):
        if not isinstance(item, str): errors.append(f"{field}[{index}] must be a string"); continue
        text, marker = item.strip(), _normalize_text(item)
        if not text: errors.append(f"{field}[{index}] cannot be blank"); continue
        if marker not in seen: seen.add(marker); result.append(text)
    if errors: raise TeamPreferenceValidationError(errors)
    return result


def _optional_text(value: Any, field: str) -> str | None:
    if value is None or value == "": return None
    if not isinstance(value, str) or not value.strip(): raise TeamPreferenceValidationError([f"{field} must be a non-blank string"])
    return value.strip()


def validate_team_preference(value: Any, *, include_display_name_alias: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping): raise TeamPreferenceValidationError(["team entry must be an object"])
    team_value = value.get("canonical_name", value.get("team", value.get("name")))
    if not isinstance(team_value, str) or not team_value.strip(): raise TeamPreferenceValidationError(["team name cannot be blank"])
    team = team_value.strip()
    aliases = _strict_string_list(value.get("aliases"), "aliases")
    if include_display_name_alias and not any(_normalize_text(a) == _normalize_text(team) for a in aliases): aliases.insert(0, team)
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool): raise TeamPreferenceValidationError(["enabled must be true or false"])
    return {"team": team, "canonical_name": team, "aliases": aliases,
            "preferred_terms": _strict_string_list(value.get("preferred_terms", value.get("preferred_broadcaster_terms")), "preferred_terms"),
            "avoid_terms": _strict_string_list(value.get("avoid_terms", value.get("disfavored_terms")), "avoid_terms"),
            "sport": _optional_text(value.get("sport"), "sport"), "league": _optional_text(value.get("league"), "league"), "enabled": enabled}


def validate_favorite_teams(value: Any, *, include_display_name_alias: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, list): raise TeamPreferenceValidationError(["favorite teams must be a JSON array"])
    teams, errors, seen = [], [], {}
    for i, item in enumerate(value):
        try: team = validate_team_preference(item, include_display_name_alias=include_display_name_alias)
        except TeamPreferenceValidationError as exc: errors.extend(f"teams[{i}]: {x}" for x in exc.errors); continue
        marker = _normalize_text(team["team"])
        if marker in seen: errors.append(f'teams[{i}]: duplicate team name "{team["team"]}" (already used at index {seen[marker]})')
        else: seen[marker] = i; teams.append(team)
    if errors: raise TeamPreferenceValidationError(errors)
    return teams


def normalize_favorite_teams(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try: value = json.loads(value)
        except (TypeError, json.JSONDecodeError): return []
    if not isinstance(value, list): return []
    result, seen = [], set()
    for raw in value:
        if not isinstance(raw, Mapping): continue
        name = str(raw.get("canonical_name") or raw.get("team") or raw.get("name") or "").strip(); marker = _normalize_text(name)
        if not marker or marker in seen: continue
        seen.add(marker); enabled = raw.get("enabled", True)
        if isinstance(enabled, str): enabled = enabled.strip().casefold() not in ("false", "0", "no", "off", "")
        result.append({"team": name, "canonical_name": name, "aliases": _string_list(raw.get("aliases")),
            "preferred_terms": _string_list(raw.get("preferred_terms", raw.get("preferred_broadcaster_terms"))), "avoid_terms": _string_list(raw.get("avoid_terms", raw.get("disfavored_terms"))),
            "sport": str(raw["sport"]).strip() if raw.get("sport") else None, "league": str(raw["league"]).strip() if raw.get("league") else None, "enabled": bool(enabled)})
    return result


def _flatten_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str): yield value
    elif isinstance(value, Mapping):
        for child in value.values(): yield from _flatten_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value: yield from _flatten_strings(child)


def _metadata_text(record: Mapping[str, Any], json_fields: Iterable[str]) -> str:
    values = []
    for key, value in record.items():
        if value is None: continue
        if key in json_fields and isinstance(value, str):
            try: value = json.loads(value)
            except (TypeError, json.JSONDecodeError): pass
        values.extend(_flatten_strings(value))
    return " | ".join(values)


def _event_text(event: Mapping[str, Any]) -> str:
    fields = {k:v for k,v in event.items() if k in {"title","title_brief","synopsis","synopsis_brief","channel_name","classification_json","genres_json","content_segments_json","raw_attributes_json","sport","league","home_team","away_team","team_names","teams"} or "team" in k.casefold()}
    return _metadata_text(fields, ("classification_json","genres_json","content_segments_json","raw_attributes_json"))


def _context_matches(event: Mapping[str, Any], team: Mapping[str, Any]) -> tuple[bool, str | None]:
    for key in ("sport", "league"):
        actual_value = event.get(key)
        if actual_value is None:
            for json_key in ("classification_json", "raw_attributes_json"):
                raw = event.get(json_key)
                if isinstance(raw, str):
                    try: raw = json.loads(raw)
                    except (TypeError, json.JSONDecodeError): continue
                if isinstance(raw, Mapping) and raw.get(key) is not None:
                    actual_value = raw.get(key)
                    break
        wanted, actual = _normalize_text(team.get(key)), _normalize_text(actual_value)
        if wanted and actual and wanted != actual: return False, f'{key} "{actual_value}" does not match configured {key} "{team.get(key)}"'
    return True, None


def _safe_alias(alias: str) -> tuple[bool, str | None]:
    tokens = _normalize_text(alias).split()
    if len(tokens) < 2: return False, "ambiguous single-word alias"
    if len("".join(tokens)) < 6: return False, "alias is too short"
    return True, None


def match_favorite_teams(event: Mapping[str, Any], favorite_teams: Any) -> list[dict[str, Any]]:
    """Return only high-confidence canonical or safe phrase-alias matches."""
    text, matches = _event_text(event), []
    for team in (x for x in normalize_favorite_teams(favorite_teams) if x["enabled"]):
        context_ok, _ = _context_matches(event, team)
        if not context_ok: continue
        if _contains_term(text, team["canonical_name"]):
            matches.append({"team": team, "matched_term": team["canonical_name"], "matched_by": "canonical_name", "confidence": "high", "matched_text": team["canonical_name"]}); continue
        for alias in team["aliases"]:
            safe, _ = _safe_alias(alias)
            if safe and _contains_term(text, alias):
                matches.append({"team": team, "matched_term": alias, "matched_by": "phrase_alias", "confidence": "high", "matched_text": alias}); break
    return matches


def rejected_favorite_matches(event: Mapping[str, Any], favorite_teams: Any) -> list[dict[str, Any]]:
    text, rejected = _event_text(event), []
    for team in (x for x in normalize_favorite_teams(favorite_teams) if x["enabled"]):
        context_ok, context_reason = _context_matches(event, team)
        if not context_ok: rejected.append({"team": team["team"], "reason": context_reason}); continue
        if _contains_term(text, team["canonical_name"]): continue
        for alias in team["aliases"]:
            if _contains_term(text, alias):
                safe, reason = _safe_alias(alias)
                if not safe: rejected.append({"team": team["team"], "matched_term": alias, "reason": f"{reason}; canonical team identity not matched"})
    return rejected


def find_favorite_teams(event: Mapping[str, Any], favorite_teams: Any) -> list[dict[str, Any]]:
    return [match["team"] for match in match_favorite_teams(event, favorite_teams)]


def _split_matchup(title: str) -> tuple[list[str], str | None]:
    match = _MATCHUP_RE.search(str(title or ""))
    if not match: return [], None
    left, right = str(title)[:match.start()].strip(" -|:"), str(title)[match.end():].strip(" -|:")
    return ([left, right], (match.group(1) or "-").lower().rstrip(".")) if left and right else ([], None)


def score_team_affinity(event: Mapping[str, Any], playable: Mapping[str, Any], favorite_teams: Any) -> dict[str, Any]:
    matches = match_favorite_teams(event, favorite_teams); teams = [match["team"] for match in matches]
    result = {"score": 0, "matched_teams": [team["team"] for team in teams], "event_matches": [{"team":match["team"]["team"], "matched_term":match["matched_term"], "matched_by":match["matched_by"], "confidence":match["confidence"], "matched_text":match["matched_text"]} for match in matches], "rejected_matches": rejected_favorite_matches(event, favorite_teams), "reasons": []}
    if not teams: return result
    playable_text = _metadata_text({k:playable.get(k) for k in ("service_name","logical_service","provider","title","feed_name","feed_type","stream_metadata_json","category_name","subcategory_name","network") if playable.get(k) is not None}, ("stream_metadata_json",))
    participants, separator = _split_matchup(str(event.get("title") or event.get("title_brief") or ""))
    favorite_indexes = {i for i,p in enumerate(participants) if any(_contains_term(p,t["canonical_name"]) or any(_safe_alias(a)[0] and _contains_term(p,a) for a in t["aliases"]) for t in teams)}
    hits = {i for i,p in enumerate(participants) if _contains_term(playable_text,p)}; favorite_hits, opponent_hits = hits & favorite_indexes, hits - favorite_indexes
    named = any(_contains_term(playable_text,t["canonical_name"]) for t in teams)
    if (favorite_hits and not opponent_hits) or (not participants and named): result["score"] += TEAM_FEED_SCORE; result["reasons"].append({"score":TEAM_FEED_SCORE,"reason":"favorite team feed"})
    elif opponent_hits and not favorite_hits: result["score"] += OPPONENT_FEED_SCORE; result["reasons"].append({"score":OPPONENT_FEED_SCORE,"reason":"opponent-specific feed"})
    preferred = next((term for t in teams for term in t["preferred_terms"] if _contains_term(playable_text,term)), None)
    if preferred: result["score"] += PREFERRED_TERM_SCORE; result["reasons"].append({"score":PREFERRED_TERM_SCORE,"reason":f'preferred broadcaster/feed "{preferred}"'})
    feed_type = _normalize_text(playable.get("feed_type"))
    if feed_type in ("home","away") and separator in ("at","@") and favorite_indexes:
        roles = {"away" if i == 0 else "home" for i in favorite_indexes}; score = TEAM_ROLE_SCORE if feed_type in roles else OPPONENT_FEED_SCORE
        result["score"] += score; result["reasons"].append({"score":score,"reason":f'{"favorite team" if score > 0 else "opponent"} {feed_type} feed'})
    elif feed_type in ("national","neutral"): result["score"] += NEUTRAL_FEED_SCORE; result["reasons"].append({"score":NEUTRAL_FEED_SCORE,"reason":f"{feed_type} feed"})
    avoid = next((term for t in teams for term in t["avoid_terms"] if _contains_term(playable_text,term)), None)
    if avoid: result["score"] += AVOID_TERM_SCORE; result["reasons"].append({"score":AVOID_TERM_SCORE,"reason":f'explicit avoid term "{avoid}"'})
    return result
