"""V3.5 · 내 캐릭터 · 그 순간의 행동 (read-only, raid-first personal context).

NEVER determines personal blame, a personal mistake or a DPS loss.
Reads only small saved JSON (mechanic review already loaded by the GUI, the
Try's facts.json, and the read-only audit file).  No raw.log / .wcla / Drive
access and no re-analysis on the Tk thread.

Audit rows (raw-verified "cause unknown" deaths) attach to a Try only on an
exact identity: encounterID + difficultyID + full start timestamp + player,
and sourceKey when the row has one.  A row WITHOUT sourceKey (plain
live_sessions Try) additionally needs a facts.json death at the same time
(+-5 ms).  Try numbers are never used for matching.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

MAX_JSON_BYTES = 2_000_000
PLAYER = "내 캐릭터"
TITLE = "내 캐릭터 · 그 순간의 행동"
NO_BLAME_LINE = "시전·차단·왕파는 기존 상세 기록 참조 · 개인 실수/DPS 손실 자동 판정 없음"

_JSON_CACHE: dict[str, tuple[tuple[int, int], dict | None]] = {}
_JSON_CACHE_MAX = 64


def _short(s) -> str:
    return str(s or "").split("-")[0]


def _read_small_json(path):
    """Small JSON dict or None.  Cached by (mtime, size): Try switching re-reads nothing."""
    if not path:
        return None
    try:
        p = Path(str(path))
        st = p.stat()
        if not p.is_file() or st.st_size > MAX_JSON_BYTES:
            return None
        sig = (st.st_mtime_ns, st.st_size)
        key = str(p)
        hit = _JSON_CACHE.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
        x = json.loads(p.read_text(encoding="utf-8-sig"))
        x = x if isinstance(x, dict) else None
        if len(_JSON_CACHE) >= _JSON_CACHE_MAX:
            _JSON_CACHE.clear()
        _JSON_CACHE[key] = (sig, x)
        return x
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def _exact_encounter(a, b) -> bool:
    """Strict on encounter, difficulty and full start timestamp, never Try number."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if not a.get("startTimestamp") or not b.get("startTimestamp"):
        return False
    if str(a["startTimestamp"]).strip() != str(b["startTimestamp"]).strip():
        return False
    try:
        return (int(a["encounterID"]) == int(b["encounterID"]) and
                int(a["difficultyID"]) == int(b["difficultyID"]))
    except (KeyError, ValueError, TypeError):
        return False


def _parse_ts(value):
    s = str(value or "").strip()
    for fmt in ("%m/%d/%Y %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _same_death_time(a, b) -> bool:
    x, y = _parse_ts(a), _parse_ts(b)
    if x is None or y is None:
        return False
    return abs((x - y).total_seconds()) <= 0.005


def _names(record) -> set[str]:
    return {_short(n) for n in (record or {}).get("players", []) or []}


def _audit_for(row, player, audit):
    """(entry, needs_death_match) for exactly one matching audit row, else (None, False)."""
    if not isinstance(audit, dict):
        return None, False
    key = str(row.get("sourceKey") or "")
    found = []
    for e in audit.get("entries") or []:
        if not isinstance(e, dict):
            continue
        if e.get("rawCauseStatus") != "RAW_VERIFIED_CAUSE_UNKNOWN":
            continue
        if _short(e.get("player")) != player or not _exact_encounter(e, row):
            continue
        if key and e.get("sourceKey") != key:
            continue
        found.append(e)
    if len(found) != 1:
        return None, False
    return found[0], not key


def _review_for_row(row, review):
    rev = review if isinstance(review, dict) else {}
    if not rev:
        rev = row.get("mechanicReview") if isinstance(row.get("mechanicReview"), dict) else {}
    ident = rev.get("encounter") if isinstance(rev.get("encounter"), dict) else {}
    if ident:
        if ident.get("startTimestamp") and row.get("startTimestamp") and not _exact_encounter(ident, row):
            return {}
        for fld in ("encounterID", "difficultyID"):
            if ident.get(fld) is not None and row.get(fld) is not None and str(ident.get(fld)) != str(row.get(fld)):
                return {}
    return rev


def _format_defensive_names(names) -> str:
    """Group every observed event name from validated facts.json, in original order.

    One facts defensive entry counts as one recorded event; no inference from UI.
    """
    counts = {}
    for name in names:
        name = str(name or '').strip()
        if name:
            counts[name] = counts.get(name, 0) + 1
    return ', '.join(f'{name} ×{amount}' if amount > 1 else name
                     for name, amount in counts.items())


def build_context(row, mechanic_review=None, *, player=PLAYER, audit=None, facts=None) -> dict:
    """Independent presentation lines.  fault is ALWAYS 'UNDETERMINED'."""
    row = row or {}
    p = _short(player)
    if not row or p != PLAYER:
        return {"visible": False, "state": "NO_ROW", "lines": [], "fault": "UNDETERMINED"}
    if row.get("isFake") or row.get("isReset") or row.get("resetExcluded"):
        return {"visible": True, "state": "EXCLUDED", "fault": "UNDETERMINED",
                "lines": ["리셋/가짜 풀 · 개인 판정하지 않음", "원본 사실은 기존 상세에서 확인", "새로운 책임 판정 없음"]}

    rev = _review_for_row(row, mechanic_review)
    f = rev.get("firstMistake") or rev.get("firstFailure") or {}
    f = f if isinstance(f, dict) else {}
    major = rev.get("majorIncident") or {}
    major = major if isinstance(major, dict) else {}

    personal = f if p in _names(f) else (major if p in _names(major) else {})
    if personal:
        if personal.get("evidenceLevel") == "verified":
            involvement = "검증된 공대 사고 관련 대상 · 개인 책임 미판정"
        else:
            involvement = "공대 사고 후보 관련 대상 기록 · 개인 책임 미판정"
    elif f or major:
        involvement = "공대 사고 발생 · 내 캐릭터 개인 관련성 미확정"
    else:
        involvement = "공대 첫 실수 미확정 · 개인 책임 판단 없음"

    if facts is None:
        facts = _read_small_json(row.get("factsJson"))
    facts_ok = (isinstance(facts, dict) and _exact_encounter(facts.get("encounter"), row)
                and _short(facts.get("player")) == p and facts.get("playerFound") is not False)
    deaths = [d for d in (facts.get("playerDeaths") or []) if isinstance(d, dict)] if facts_ok else []
    if facts_ok:
        history = "개인 사망 %d회" % len(deaths)
        # No cause from finalHit: v3.3.2 found gaps in raw damage coverage.
        defs = [str(d.get("spellName")) for d in (facts.get("defensives") or []) if isinstance(d, dict) and d.get("spellName")]
        if defs:
            history += " · 생존기 " + _format_defensive_names(defs)
        cds = [str(c.get("spellName")) for c in (facts.get("cooldowns") or []) if isinstance(c, dict) and c.get("spellName")]
        if cds:
            history += " · 쿨기 관측 " + ", ".join(cds[:2])
    else:
        history = "개인 이벤트 저장 사실 미확인 (원본 실시간 재분석 안 함)"

    audit_row, needs_death = _audit_for(row, p, audit)
    if audit_row is not None:
        death_known = any(_same_death_time(d.get("timestamp"), audit_row.get("deathTimestamp")) for d in deaths)
        if (needs_death or facts_ok) and not death_known:
            audit_row = None          # same pull but no matching saved death: not enough
    if audit_row is not None:
        history = "개인 사망 원본 검증 완료 · 직접 사인 미확정 (마지막 치명타 미관측 · UNKNOWN 유지)"
        if audit_row.get("lastObservedSpell"):
            history += " · 마지막 관측 피해 " + str(audit_row["lastObservedSpell"])
            if isinstance(audit_row.get("gapSec"), (int, float)):
                history += f" ({audit_row['gapSec']:.3f}초 전)"

    return {"visible": True,
            "state": "RAW_CAUSE_UNRESOLVED" if audit_row is not None else ("FACTS_MATCHED" if facts_ok else "NO_MATCHED_FACTS"),
            "lines": [involvement, history, NO_BLAME_LINE],
            "fault": "UNDETERMINED",
            "isAuditedUnknown": audit_row is not None,
            "factSource": "cached_facts_json" if facts_ok else "none"}
