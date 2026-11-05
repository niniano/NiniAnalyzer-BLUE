"""B37.4.36 · raid/check difficulty scope helpers.

This module is presentation/navigation metadata only.  It does not change boss
mechanic judgments and deliberately keeps unknown legacy rows visible.
"""
from __future__ import annotations

from typing import Any

# Legacy 10/25 N/H, LFR, current Normal/Heroic/Mythic and Timewalking raid.
RAID_DIFFICULTY_IDS = frozenset({3, 4, 5, 6, 7, 9, 14, 15, 16, 17, 33, 233})
HEROIC_DIFFICULTY_IDS = frozenset({4, 15})
MYTHIC_DIFFICULTY_IDS = frozenset({5, 16})
FLEX_MYTHIC_DIFFICULTY_IDS = frozenset({233})
NYMRISSA_MYTHIC_DIFFICULTY_IDS = frozenset(MYTHIC_DIFFICULTY_IDS | FLEX_MYTHIC_DIFFICULTY_IDS)

# Only mechanics whose difficulty difference is explicitly confirmed in the
# shipped BossMechanic rules (mythicOnly / mythicOnlyJudgment) belong here.
# The value is exposed to the UI as per-check ``difficultyIDs``.
CHECK_DIFFICULTY_IDS = {
    "sszorak": {
        "mutilate_under_soak": tuple(sorted(MYTHIC_DIFFICULTY_IDS)),
        "serpents_fury_enrage": tuple(sorted(MYTHIC_DIFFICULTY_IDS)),
    },
    "vashnik": {
        "malignance": tuple(sorted(MYTHIC_DIFFICULTY_IDS)),
    },
    "nymrissa": {
        # Nymrissa Flexible Mythic persists as difficultyID 233 in our live archive.
        "frost_burst": tuple(sorted(NYMRISSA_MYTHIC_DIFFICULTY_IDS)),
        "water_jet": tuple(sorted(NYMRISSA_MYTHIC_DIFFICULTY_IDS)),
        "waterfog_shield": tuple(sorted(NYMRISSA_MYTHIC_DIFFICULTY_IDS)),
    },
}


def difficulty_id(value: Any) -> int | None:
    """Return a numeric difficulty from a row, review encounter, or scalar."""
    if isinstance(value, dict):
        if value.get("difficultyID") is not None:
            value = value.get("difficultyID")
        elif isinstance(value.get("encounter"), dict):
            value = value["encounter"].get("difficultyID")
        else:
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

# Presentation labels used by the live GUI.  Flexible Mythic (233) is how
# Nymrissa Mythic is persisted in the current live archive, so it must not be
# shown as the raw placeholder ``d233``.  Legacy 4/5 follow this module's
# existing heroic/mythic scope constants.
_DISPLAY_DIFFICULTY_NAMES = {
    4: "영웅",
    5: "신화",
    14: "일반",
    15: "영웅",
    16: "신화",
    17: "공찾",
    233: "신화",
}


def display_difficulty_name(value: Any, fallback: str = "") -> str:
    """Return a stable user-facing difficulty label.

    Stored rows normally already carry a localized ``difficultyName``.  Known
    raid IDs are normalized so archive placeholders such as ``d233`` do not
    leak into the UI.  Unknown names are preserved, and unknown numeric rows
    fall back to their persisted ``d<ID>`` form when no explicit fallback is
    supplied.  This is presentation-only; it does not change difficulty scope
    or mechanic judgment.
    """
    row = value if isinstance(value, dict) else {}
    name = ""
    if isinstance(row, dict):
        name = str(row.get("difficultyName") or "").strip()
        if not name and isinstance(row.get("encounter"), dict):
            name = str(row["encounter"].get("difficultyName") or "").strip()

    diff = difficulty_id(value)
    normalized = _DISPLAY_DIFFICULTY_NAMES.get(diff)
    if normalized:
        return normalized
    if name:
        return name
    fallback_text = str(fallback or "").strip()
    if fallback_text:
        return fallback_text
    return f"d{diff}" if diff is not None else ""


def is_raid_row(row: dict | None) -> bool:
    """Whether a persisted row belongs in default raid navigation.

    Missing difficulty is kept for backwards compatibility.  A known numeric
    non-raid difficulty (for example delve 208 or Mythic+ 8) is hidden from the
    raid navigator but remains untouched on disk/archive.
    """
    row = row or {}
    diff = difficulty_id(row)
    name = str(row.get("difficultyName") or "").strip().lower()
    if "신화+" in name or "mythic+" in name or "mythic plus" in name:
        return False
    if diff is None:
        return True
    return diff in RAID_DIFFICULTY_IDS


def check_difficulty_ids(boss_key: Any, check_key: Any) -> tuple[int, ...]:
    return tuple(CHECK_DIFFICULTY_IDS.get(str(boss_key or ""), {}).get(str(check_key or ""), ()))


def difficulty_matches(allowed: Any, current: Any) -> bool | None:
    """Return True/False for known difficulty, None when scope cannot be decided."""
    current_id = difficulty_id(current)
    ids = tuple(int(x) for x in (allowed or ()) if str(x).strip())
    if not ids or current_id is None:
        return None
    return current_id in ids


def is_mythic_only(allowed: Any) -> bool:
    try:
        ids = {int(x) for x in (allowed or ())}
    except (TypeError, ValueError):
        return False
    return bool(ids) and ids.issubset(MYTHIC_DIFFICULTY_IDS | FLEX_MYTHIC_DIFFICULTY_IDS)


def is_heroic(value: Any) -> bool:
    return difficulty_id(value) in HEROIC_DIFFICULTY_IDS
