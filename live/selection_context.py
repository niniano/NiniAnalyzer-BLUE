"""B33 shared selection identity helpers.

A UI selection is identified by stable encounter metadata, never by a row/list
index.  The same encounter can exist on multiple raid dates, characters and
specs, so all five dimensions are part of the key.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _intish(value: Any):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


RAID_DAY_CUTOFF_HOUR = 5


def _raid_session_date(date_value: datetime, hour: int | None) -> str:
    """Map a calendar timestamp to the raid-night date.

    Encounters before 05:00 belong to the previous raid night.  Date-only
    values deliberately stay on their calendar date because there is no clock
    information on which to apply the cutoff.
    """
    if hour is not None and int(hour) < RAID_DAY_CUTOFF_HOUR:
        date_value = date_value - timedelta(days=1)
    return date_value.strftime("%Y-%m-%d")


_SESSION_DATE_CACHE: dict = {}


def session_date_from_timestamp(value: Any) -> str | None:
    """B38 SPEED2: memoized (pure function of the text + cutoff hour)."""
    text = str(value or "").strip()
    if not text:
        return None
    key = (text, RAID_DAY_CUTOFF_HOUR)
    if key in _SESSION_DATE_CACHE:
        return _SESSION_DATE_CACHE[key]
    result = _session_date_from_text(text)
    if len(_SESSION_DATE_CACHE) > 50000:
        _SESSION_DATE_CACHE.clear()
    _SESSION_DATE_CACHE[key] = result
    return result


def _session_date_from_text(text: str) -> str | None:

    # CombatLog metadata is usually M/D/YYYY HH:MM:SS.ssss; generated metadata
    # can be ISO.  Parse the date and clock separately so arbitrary fractional
    # seconds / timezone suffixes do not affect the raid-day cutoff.
    date_token = re.match(r"^(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2})", text)
    if date_token:
        raw_date = date_token.group(1)
        parsed = None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(raw_date, fmt)
                break
            except ValueError:
                pass
        if parsed is not None:
            tail = text[date_token.end():]
            tm = re.search(r"(?:T|\s)(\d{1,2}):(\d{2})(?::(\d{2}))?", tail)
            hour = int(tm.group(1)) if tm else None
            return _raid_session_date(parsed, hour)

    head = text.split()[0]
    return head or None


def session_date_from_log_path(path: Path | str | None) -> str | None:
    if not path:
        return None
    name = Path(path).name
    # Standard WoW file names include the client-start clock after the date.
    # If the client was launched after midnight but before 05:00, keep that log
    # under the previous raid night as well.  Older/short names without a clock
    # retain their literal date because the cutoff cannot be inferred.
    m = re.search(
        r"WoWCombatLog-(\d{2})(\d{2})(\d{2})(?:_(\d{2})(\d{2})(\d{2}))?",
        name,
    )
    if not m:
        return None
    mm, dd, yy, hh, _mi, _ss = m.groups()
    try:
        parsed = datetime.strptime(f"20{yy}-{mm}-{dd}", "%Y-%m-%d")
    except ValueError:
        return None
    return _raid_session_date(parsed, int(hh) if hh is not None else None)


def short_player(value: Any) -> str:
    return str(value or "").split("-")[0].strip()


def boss_context_key(boss: dict | None, session_date: str | None, player: str | None):
    row = boss or {}
    return (
        _intish(row.get("encounterID")),
        _intish(row.get("difficulty") if row.get("difficulty") is not None else row.get("difficultyID")),
        str(session_date or ""),
        short_player(player),
        _intish(row.get("specID")),
    )


def live_context_key(row: dict | None, fallback_player: str | None = None, fallback_spec_id: Any = None):
    data = row or {}
    player = data.get("factsPlayer") or data.get("livePlayer") or fallback_player
    spec = (
        data.get("factsSpecID")
        if data.get("factsSpecID") is not None
        else data.get("liveSpecID")
        if data.get("liveSpecID") is not None
        else data.get("specID")
        if data.get("specID") is not None
        else fallback_spec_id
    )
    return (
        _intish(data.get("encounterID")),
        _intish(data.get("difficultyID") if data.get("difficultyID") is not None else data.get("difficulty")),
        str(session_date_from_timestamp(data.get("startTimestamp")) or data.get("sessionDate") or ""),
        short_player(player),
        _intish(spec),
    )


def context_dict(key) -> dict:
    key = tuple(key or (None, None, "", "", None))
    if len(key) < 5:
        key = key + (None,) * (5 - len(key))
    return {
        "encounterID": key[0],
        "difficultyID": key[1],
        "sessionDate": key[2] or None,
        "player": key[3] or None,
        "specID": key[4],
    }


def context_tuple_from_state(state: dict | None):
    state = state or {}
    return (
        _intish(state.get("encounterID")),
        _intish(state.get("difficultyID")),
        str(state.get("sessionDate") or ""),
        short_player(state.get("player")),
        _intish(state.get("specID")),
    )



def new_selection_state(player: str | None = None) -> dict:
    """Create the shared GUI selection state without list-position identity."""
    return {
        "encounterID": None,
        "difficultyID": None,
        "sessionDate": None,
        "player": short_player(player) or None,
        "specID": None,
        "liveTryKey": None,
        "page": "session",
    }

def selection_tokens_match(expected, current) -> bool:
    """Return True only for the exact selection that launched a background job."""
    return expected is not None and expected == current
