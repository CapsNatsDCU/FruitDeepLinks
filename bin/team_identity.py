"""Conservative names for team identities derived from provider observations."""

from __future__ import annotations

import re
from typing import Any


_RANK = re.compile(r"^#\d{1,3}\s+")
_MATCHUP = re.compile(r"\s(?:vs\.?|v\.?|at|@)\s", re.IGNORECASE)
_ISO_DATE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.IGNORECASE)
_CALENDAR_DATE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\b",
    re.IGNORECASE,
)
_SLASH_DATE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_CHANNEL_NUMBER = re.compile(r"^\d{2}\s+(?=\D)")
_PROGRAM_LABEL = re.compile(
    r"\b(?:broadcast|halftime|pregame|postgame|highlights?|replay|recap|cutdown|"
    r"episode|coverage)\b",
    re.IGNORECASE,
)


def clean_team_name(value: Any) -> str | None:
    """Keep a team label; reject provider titles masquerading as participants.

    A leading poll rank is display noise. Matchups, timestamps, and composite
    channel labels are not team names. This does not resolve abbreviations or
    merge existing catalog IDs.
    """
    if not isinstance(value, str):
        return None
    name = _RANK.sub("", " ".join(value.split()))
    # A two-digit channel slot is presentation metadata, not part of a team.
    # A colon still marks a composite program label, so do not try to infer
    # its participant from the text after it.
    name = _CHANNEL_NUMBER.sub("", name)
    if not 2 <= len(name) <= 80 or not any(character.isalpha() for character in name):
        return None
    if (_MATCHUP.search(name) or _ISO_DATE.search(name) or _CLOCK.search(name)
            or _CALENDAR_DATE.search(name) or _SLASH_DATE.search(name)
            or _PROGRAM_LABEL.search(name) or re.search(r"#\d+", name)
            or re.search(r"[:•|]", name) or re.search(r"\s[-·]\s", name)):
        return None
    depth = 0
    for character in name:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                return None
    return name if depth == 0 else None
