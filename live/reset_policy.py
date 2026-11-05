"""Conservative reset-pull classification shared by Live review and archive indexes.

B37.3.5 changes the old "all non-kills <=30s are resets" rule.  A short wipe can
be a real progression pull (tank death, early mechanic failure, etc.), so only
very short pulls with no death and almost no player->hostile engagement are
excluded automatically.

When legacy rows do not contain the new evidence counters, only sub-second pulls
are auto-excluded.  This keeps obvious 0.02s reconnect/reset records out without
silently hiding genuine 5-30s early wipes.
"""
from __future__ import annotations

RESET_MAX_DURATION_SEC = 5.0
LEGACY_RESET_MAX_DURATION_SEC = 1.0
RESET_MAX_HOSTILE_DAMAGE_EVENTS = 1
EARLY_WIPE_MAX_DURATION_SEC = 30.0
RESET_REASON = "very-short-no-death-no-engagement"
LEGACY_RESET_REASON = "legacy-subsecond-reset"
NORMAL_OVERRIDE = "normal"


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def is_kill(encounter: dict | None) -> bool:
    row = encounter or {}
    if _as_int(row.get("success")) == 1:
        return True
    result = str(row.get("result") or "").strip().upper()
    return result in {"KILL", "KILLED", "킬", "SUCCESS"}


def has_normal_override(encounter: dict | None) -> bool:
    row = encounter or {}
    return str(row.get("analysisOverride") or row.get("resetOverride") or "").strip().lower() == NORMAL_OVERRIDE


def _death_count(row: dict) -> int | None:
    for key in ("deathCount", "playerDeathCount"):
        value = _as_int(row.get(key))
        if value is not None:
            return max(0, value)
    audit = row.get("resetAudit") if isinstance(row.get("resetAudit"), dict) else {}
    value = _as_int(audit.get("deathCount"))
    return max(0, value) if value is not None else None


def _hostile_damage_events(row: dict) -> int | None:
    for key in ("hostileDamageEventCount", "raidToHostileDamageEventCount", "bossDamageEventCount"):
        value = _as_int(row.get(key))
        if value is not None:
            return max(0, value)
    audit = row.get("resetAudit") if isinstance(row.get("resetAudit"), dict) else {}
    for key in ("hostileDamageEventCount", "raidToHostileDamageEventCount", "bossDamageEventCount"):
        value = _as_int(audit.get(key))
        if value is not None:
            return max(0, value)
    return None


def reset_classification(encounter: dict | None, max_duration_sec: float = RESET_MAX_DURATION_SEC) -> dict:
    row = encounter or {}
    duration = _as_float(row.get("durationSec"))
    deaths = _death_count(row)
    hostile_damage_events = _hostile_damage_events(row)
    evidence_complete = deaths is not None and hostile_damage_events is not None

    if is_kill(row):
        reset = False
        reason = "kill"
    elif duration is None or duration < 0:
        reset = False
        reason = "duration_unavailable"
    elif evidence_complete:
        reset = (
            duration <= float(max_duration_sec)
            and deaths == 0
            and hostile_damage_events <= RESET_MAX_HOSTILE_DAMAGE_EVENTS
        )
        if reset:
            reason = RESET_REASON
        elif duration <= EARLY_WIPE_MAX_DURATION_SEC:
            reason = "short_real_pull_evidence"
        else:
            reason = "normal_duration"
    else:
        # Legacy summaries/captures predate the evidence counters. Be deliberately
        # conservative: only the obvious sub-second records are hidden.
        reset = duration <= LEGACY_RESET_MAX_DURATION_SEC
        reason = LEGACY_RESET_REASON if reset else ("short_legacy_kept" if duration <= EARLY_WIPE_MAX_DURATION_SEC else "normal_duration")

    early_wipe = bool(
        not reset
        and not is_kill(row)
        and duration is not None
        and 0 <= duration <= EARLY_WIPE_MAX_DURATION_SEC
    )
    return {
        "isReset": bool(reset),
        "earlyWipe": early_wipe,
        "reason": reason,
        "durationSec": duration,
        "deathCount": deaths,
        "hostileDamageEventCount": hostile_damage_events,
        "evidenceComplete": evidence_complete,
        "resetMaxDurationSec": float(max_duration_sec),
        "legacyResetMaxDurationSec": LEGACY_RESET_MAX_DURATION_SEC,
        "maxHostileDamageEvents": RESET_MAX_HOSTILE_DAMAGE_EVENTS,
        "earlyWipeMaxDurationSec": EARLY_WIPE_MAX_DURATION_SEC,
    }


def is_auto_reset_pull(encounter: dict | None, max_duration_sec: float = RESET_MAX_DURATION_SEC) -> bool:
    return bool(reset_classification(encounter, max_duration_sec=max_duration_sec)["isReset"])


def is_reset_pull(encounter: dict | None, max_duration_sec: float = RESET_MAX_DURATION_SEC) -> bool:
    row = encounter or {}
    if has_normal_override(row):
        return False
    return is_auto_reset_pull(row, max_duration_sec=max_duration_sec)


def is_early_wipe(encounter: dict | None) -> bool:
    row = encounter or {}
    if has_normal_override(row):
        # Manual normal override should still be allowed to retain the useful
        # early-wipe label when the duration is short.
        duration = _as_float(row.get("durationSec"))
        return bool(not is_kill(row) and duration is not None and 0 <= duration <= EARLY_WIPE_MAX_DURATION_SEC)
    return bool(reset_classification(row)["earlyWipe"])


def reset_metadata(encounter: dict | None) -> dict:
    row = encounter or {}
    cls = reset_classification(row)
    override = has_normal_override(row)
    reset = bool(cls["isReset"] and not override)
    return {
        "resetPull": reset,
        "autoResetPull": bool(cls["isReset"]),
        "earlyWipe": bool(cls["earlyWipe"] or (override and is_early_wipe(row))),
        "analysisOverride": NORMAL_OVERRIDE if override else row.get("analysisOverride"),
        "resetReason": cls["reason"] if reset else None,
        "resetMaxDurationSec": RESET_MAX_DURATION_SEC,
        "resetAudit": cls,
    }
