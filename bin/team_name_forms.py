"""Explicit, name-only team forms shared by offline matching and AI context."""
from __future__ import annotations

import re


CITY_ABBREVIATIONS = {
    "NY": "New York", "LA": "Los Angeles", "SF": "San Francisco",
    "KC": "Kansas City", "TB": "Tampa Bay", "LV": "Las Vegas",
    "SD": "San Diego", "SJ": "San Jose", "DC": "Washington",
}
EXACT_TEAM_ABBREVIATIONS = {
    "BC": "Boston College", "FSU": "Florida State", "MIZ": "Missouri",
    "MSST": "Mississippi State", "NCSU": "NC State", "ND": "Notre Dame",
    "OKST": "Oklahoma State", "PITT": "Pitt", "TA&M": "Texas A&M",
    "TENN": "Tennessee", "UGA": "Georgia", "UNC": "North Carolina",
}


def strip_parentheses(name: str) -> str:
    """Remove every balanced parenthetical span, including nested spans."""
    depth = 0
    outside = []
    for char in name:
        if char == "(":
            depth += 1
            if depth == 1:
                outside.append(" ")
        elif char == ")":
            if depth == 0:
                return ""
            depth -= 1
        elif depth == 0:
            outside.append(char)
    return " ".join("".join(outside).split()) if depth == 0 else ""


def expand_obvious_abbreviation(name: str) -> str:
    """Expand only fixed whole-name or city-prefix spellings."""
    seed = re.fullmatch(r"(#\d+\s+)?([A-Z&]{2,6})", name)
    if seed and seed.group(2) in EXACT_TEAM_ABBREVIATIONS:
        return (seed.group(1) or "") + EXACT_TEAM_ABBREVIATIONS[seed.group(2)]
    parts = name.split(maxsplit=1)
    if (len(parts) == 2 and not re.search(r"\b(?:vs|at)\b|[•/]", parts[1], re.I)
            and parts[0].upper().rstrip(".") in CITY_ABBREVIATIONS):
        return CITY_ABBREVIATIONS[parts[0].upper().rstrip(".")] + " " + parts[1]
    return name


def obvious_abbreviations_for_name(name: str) -> list[str]:
    """Give the AI small explicit alternatives without opaque catalog IDs."""
    alternatives = []
    seed = re.fullmatch(r"(#\d+\s+)?(.+)", name)
    if seed:
        for short, full in EXACT_TEAM_ABBREVIATIONS.items():
            if seed.group(2).casefold() == full.casefold():
                alternatives.append((seed.group(1) or "") + short)
    for short, full in CITY_ABBREVIATIONS.items():
        if name.casefold().startswith(full.casefold() + " "):
            suffix = name[len(full) + 1:]
            if suffix and not re.search(r"\b(?:vs|at)\b|[•/]", suffix, re.I):
                alternatives.append(short + " " + suffix)
    return alternatives
