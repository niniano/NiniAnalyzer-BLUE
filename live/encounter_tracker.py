from __future__ import annotations

import csv
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Iterable

from .reset_policy import is_reset_pull, reset_metadata

LINE_RE = re.compile(
    r'^(?P<ts>\d{1,2}/\d{1,2}(?:/\d{2,4})?\s+\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)\s+(?P<payload>.+)$'
)

DIFFICULTY_NAMES = {
    14: "일반",
    15: "영웅",
    16: "신화",
    17: "공찾",
    8: "신화+",
}


def safe_int(value):
    try:
        return int(value)
    except Exception:
        return None


def parse_log_line(raw: str):
    m = LINE_RE.match(raw.rstrip("\r\n"))
    if not m:
        return None
    try:
        fields = next(csv.reader([m.group("payload")]))
    except Exception:
        return None
    if not fields:
        return None
    return {
        "timestamp": m.group("ts"),
        "event": fields[0].strip(),
        "fields": fields,
        "raw": raw.rstrip("\r\n"),
    }


# B31-perf: WoW combat-log timestamps are always "M/D/YYYY HH:MM:SS.ffff"
# (or 2-digit year).  strptime() tried up to six formats per line and was the
# single largest cost of parsing (~1/3 of total).  The fast path below parses
# the fixed layout directly with identical semantics (%f right-pads the
# fraction to microseconds, %y maps 69-99 -> 19xx and 00-68 -> 20xx) and is
# memoised because consecutive log lines usually share a timestamp.  Any other
# shape falls through to the original strptime loop unchanged.
_FAST_TS_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4}) (\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$")
_FAST_TS_CACHE: dict = {}
_FAST_TS_CACHE_MAX = 8192


def _fast_combat_datetime(value: str) -> datetime | None:
    hit = _FAST_TS_CACHE.get(value)
    if hit is not None:
        return hit
    m = _FAST_TS_RE.match(value)
    if not m:
        return None
    mo, d, y, hh, mi, ss, frac = m.groups()
    year = int(y)
    if len(y) == 2:
        year += 2000 if year < 69 else 1900
    micro = int(frac.ljust(6, "0")) if frac else 0
    try:
        dt = datetime(year, int(mo), int(d), int(hh), int(mi), int(ss), micro)
    except ValueError:
        return None
    if len(_FAST_TS_CACHE) >= _FAST_TS_CACHE_MAX:
        _FAST_TS_CACHE.clear()
    _FAST_TS_CACHE[value] = dt
    return dt


def parse_combat_datetime(value: str, now: datetime | None = None) -> datetime | None:
    value = (value or "").strip()
    fast = _fast_combat_datetime(value)
    if fast is not None:
        return fast
    now = now or datetime.now()
    formats = (
        "%m/%d/%Y %H:%M:%S.%f",
        "%m/%d/%y %H:%M:%S.%f",
        "%m/%d %H:%M:%S.%f",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%y %H:%M:%S",
        "%m/%d %H:%M:%S",
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt and "%y" not in fmt:
            dt = dt.replace(year=now.year)
            # New Year's edge case: Dec log viewed in early Jan or vice versa.
            if dt - now > timedelta(days=180):
                dt = dt.replace(year=now.year - 1)
            elif now - dt > timedelta(days=180):
                dt = dt.replace(year=now.year + 1)
        return dt
    return None


def encounter_duration(start_ts: str, end_ts: str) -> float | None:
    start = parse_combat_datetime(start_ts)
    end = parse_combat_datetime(end_ts)
    if not start or not end:
        return None
    delta = (end - start).total_seconds()
    if delta < 0 and delta > -86400:
        delta += 86400
    return max(0.0, delta)


def _secs_between(a, b):
    """Seconds from combat-log timestamp a to b, or None if either is unparsable."""
    try:
        da = parse_combat_datetime(a)
        db = parse_combat_datetime(b)
    except Exception:
        return None
    if da is None or db is None:
        return None
    return (db - da).total_seconds()


class EncounterTracker:
    def __init__(self, recent_limit: int = 20):
        self.current: dict | None = None
        self.recent = deque(maxlen=max(1, int(recent_limit)))
        self.try_counts = defaultdict(int)
        # B33.4: try numbering is scoped to one raid-night session.  The key
        # remains (encounterID, difficultyID) for compatibility, while this map
        # tells us when a new session is allowed to reset/seed the counter.
        self._try_sessions = {}
        self.parsed_lines = 0
        self.unparsed_lines = 0

    def prepare_try_count(self, encounter_id, difficulty_id, *, session_token=None, existing_max=0):
        """Seed the next Try number from persisted pulls for one raid session.

        ``existing_max`` is the highest already-saved Try number for the same
        raid-night + encounter + difficulty.  On the same session we only ever
        raise the in-memory floor; when the session changes, the counter may
        reset to that session's persisted floor.  ``_start()`` increments once
        after this method, so a persisted max of 6 produces Try 7.
        """
        key = (safe_int(encounter_id), safe_int(difficulty_id))
        try:
            floor = max(0, int(existing_max or 0))
        except (TypeError, ValueError):
            floor = 0
        token = str(session_token or "")
        previous = self._try_sessions.get(key)
        if token and previous != token:
            self.try_counts[key] = floor
            self._try_sessions[key] = token
        else:
            self.try_counts[key] = max(int(self.try_counts.get(key, 0) or 0), floor)
            if token:
                self._try_sessions[key] = token
        return int(self.try_counts[key])

    def reset_current(self, reason: str = "reset"):
        if not self.current:
            return None
        row = dict(self.current)
        row.pop("_feignAt", None)
        row.pop("_feignPending", None)
        row.update({
            "success": None,
            "result": "중단",
            "endTimestamp": None,
            "durationSec": None,
            "endReason": reason,
            # A truncated capture may still be a genuine raid pull.  It keeps
            # its chronological Try number and can later be superseded by a
            # complete Historical capture with the same pullKey.
            "countedTry": True,
        })
        row.pop("detectedMonotonic", None)
        self.recent.appendleft(row)
        self.current = None
        return {"type": "encounter_abort", "encounter": row}

    def _start(self, parsed: dict):
        fields = parsed["fields"]
        encounter_id = safe_int(fields[1]) if len(fields) > 1 else None
        name = fields[2] if len(fields) > 2 else "알 수 없는 보스"
        difficulty_id = safe_int(fields[3]) if len(fields) > 3 else None
        group_size = safe_int(fields[4]) if len(fields) > 4 else None
        key = (encounter_id, difficulty_id)
        self.try_counts[key] += 1
        self.current = {
            "encounterID": encounter_id,
            "name": name,
            "difficultyID": difficulty_id,
            "difficultyName": DIFFICULTY_NAMES.get(difficulty_id, f"d{difficulty_id}" if difficulty_id is not None else "-"),
            "groupSize": group_size,
            "tryNo": self.try_counts[key],
            "startTimestamp": parsed["timestamp"],
            "endTimestamp": None,
            "success": None,
            "result": "진행 중",
            "durationSec": None,
            "eventCount": 1,
            "deathCount": 0,
            "hostileDamageEventCount": 0,
            "detectedMonotonic": time.monotonic(),
        }
        return {"type": "encounter_start", "encounter": dict(self.current)}

    def _end(self, parsed: dict):
        fields = parsed["fields"]
        success = safe_int(fields[5]) if len(fields) > 5 else None
        if self.current is None:
            return None
        self.current["eventCount"] += 1
        self.current["endTimestamp"] = parsed["timestamp"]
        self.current["success"] = success
        self.current["result"] = "Kill" if success == 1 else "Wipe" if success == 0 else "종료"
        self.current["durationSec"] = encounter_duration(
            self.current.get("startTimestamp") or "", parsed["timestamp"]
        )
        row = dict(self.current)
        row.pop("_feignAt", None)
        row.pop("_feignPending", None)
        # B37.3.4: reset/fake pulls are preserved as observations but do not
        # consume the user-facing Try sequence.  Numbering can only be decided
        # after ENCOUNTER_END because duration is unknown at pull start.
        row.update(reset_metadata(row))
        if is_reset_pull(row):
            key = (safe_int(row.get("encounterID")), safe_int(row.get("difficultyID")))
            current_no = safe_int(row.get("tryNo")) or 0
            if current_no and int(self.try_counts.get(key, 0) or 0) == current_no:
                self.try_counts[key] = max(0, current_no - 1)
            row["countedTry"] = False
            row["resetPull"] = True
        else:
            row["countedTry"] = True
        row.pop("detectedMonotonic", None)
        self.recent.appendleft(row)
        self.current = None
        return {"type": "encounter_end", "encounter": row}

    def feed_line(self, raw: str) -> list[dict]:
        parsed = parse_log_line(raw)
        if not parsed:
            self.unparsed_lines += 1
            return []
        self.parsed_lines += 1
        event = parsed["event"]
        out = []
        if event == "ENCOUNTER_START":
            if self.current is not None:
                aborted = self.reset_current("new-encounter-start")
                if aborted:
                    out.append(aborted)
            out.append(self._start(parsed))
        elif event == "ENCOUNTER_END":
            ended = self._end(parsed)
            if ended:
                out.append(ended)
        elif self.current is not None:
            self.current["eventCount"] += 1
            fields = parsed.get("fields") or []
            # B37.3.5 reset audit: a real early wipe must not disappear just
            # because it was shorter than the old 30s threshold.  Track only
            # cheap, robust engagement signals while the encounter is live.
            # RC4.1: Hunter Feign Death (5384) may be logged as UNIT_DIED.  Count
            # the death first; take it back only if the same player acts again
            # (no resurrection in between) within 30s.  Uncertain -> keep it.
            src_guid = str(fields[1] if len(fields) > 1 else "")
            dst_guid = str(fields[5] if len(fields) > 5 else "")
            pending = self.current.get("_feignPending")
            if pending:
                if event == "SPELL_RESURRECT":
                    pending.pop(dst_guid, None)
                cand_ts = pending.get(src_guid)
                if cand_ts is not None:
                    gap = _secs_between(cand_ts, parsed.get("timestamp"))
                    if gap is None or gap > 30.0:
                        pending.pop(src_guid, None)
                    elif ((event in {"SPELL_CAST_SUCCESS", "SPELL_CAST_START"} and gap > 0.0)
                            or (event in {"SWING_DAMAGE", "SWING_MISSED", "RANGE_DAMAGE", "RANGE_MISSED"} and gap >= 1.5)):
                        pending.pop(src_guid, None)
                        self.current["deathCount"] = max(0, int(self.current.get("deathCount") or 0) - 1)
            if event in {"SPELL_CAST_SUCCESS", "SPELL_AURA_APPLIED", "SPELL_AURA_REFRESH"} and len(fields) > 9:
                if safe_int(fields[9]) == 5384:
                    fd_guid = src_guid if event == "SPELL_CAST_SUCCESS" else dst_guid
                    if fd_guid.startswith("Player-"):
                        self.current.setdefault("_feignAt", {})[fd_guid] = parsed.get("timestamp")
            if event in {"UNIT_DIED", "UNIT_DESTROYED", "SPELL_INSTAKILL"}:
                if dst_guid.startswith("Player-"):
                    self.current["deathCount"] = int(self.current.get("deathCount") or 0) + 1
                    feign_at = self.current.get("_feignAt") or {}
                    fd_ts = feign_at.get(dst_guid)
                    gap = _secs_between(fd_ts, parsed.get("timestamp")) if fd_ts else None
                    if (gap is not None and 0.0 <= gap <= 3.0
                            and dst_guid not in (self.current.get("_feignPending") or {})):
                        self.current.setdefault("_feignPending", {})[dst_guid] = parsed.get("timestamp")
                        feign_at.pop(dst_guid, None)
            if event in {"SWING_DAMAGE", "RANGE_DAMAGE", "SPELL_DAMAGE", "SPELL_PERIODIC_DAMAGE"}:
                source_guid = fields[1] if len(fields) > 1 else ""
                dest_guid = fields[5] if len(fields) > 5 else ""
                if str(source_guid or "").startswith(("Player-", "Pet-")) and str(dest_guid or "").startswith(("Creature-", "Vehicle-")):
                    self.current["hostileDamageEventCount"] = int(self.current.get("hostileDamageEventCount") or 0) + 1
        return out

    def feed_lines(self, lines: Iterable[str]) -> list[dict]:
        out = []
        for line in lines:
            out.extend(self.feed_line(line))
        return out

    def current_elapsed(self) -> float | None:
        if not self.current:
            return None
        start = self.current.get("detectedMonotonic")
        if start is None:
            return None
        return max(0.0, time.monotonic() - float(start))
