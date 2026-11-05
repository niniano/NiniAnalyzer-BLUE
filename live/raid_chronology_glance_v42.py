"""V4.2 read-only chronology wording for the 42 *audited* Oct 13 Coiled pulls.

No judgment is created, promoted, persisted, or recomputed. No file I/O.
Callers feed the V3.7 saved index already used for the raid chronology panel.
If identity/metadata is incomplete, the V4.0.1 summary is returned unchanged.
"""
from __future__ import annotations
import math
from live.raid_chronology_v37 import _lookup


def _sec(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _clock(value):
    sec = _sec(value)
    return f"{int(sec // 60)}:{sec % 60:04.1f}" if sec is not None else "시각 미관측"


def clarify_glance(summary, row, index):
    """Return three display strings. Fail closed for every unverified pull.

    Data are the exact-V3.7-index matching first/causal/collapse observations.
    'Causal' is an observed generic mass-death *candidate*, not a wipe cause.
    """
    if not isinstance(summary, (tuple, list)) or len(summary) != 3:
        return summary
    if not isinstance(row, dict) or not isinstance(index, dict):
        return summary
    if str(row.get("result") or "").upper() != "WIPE":
        return summary
    if any(row.get(flag) for flag in ("isFake", "isReset", "resetExcluded", "mechanicReviewStale")):
        return summary
    record = _lookup(row, index)  # Exact full sourceKey / encounter / difficulty / timestamp.
    if not record:
        return summary
    if str(record.get("startTimestamp") or "").split(" ", 1)[0] != "10/13/2026":
        return summary
    if row.get("sourceKey") and str(record.get("sourceKey") or "") != str(row["sourceKey"]):
        return summary
    # No-sourceKey fallback is allowed only when V3.7 _lookup has proved unique
    # encounter+difficulty+full startTimestamp+duration within 0.005 seconds.

    first = record.get("first") or {}
    causal = record.get("causal") or {}
    collapse = record.get("collapse") or {}
    ft, ct, kt = _sec(first.get("sec")), _sec(causal.get("sec")), _sec(collapse.get("sec"))
    label = str(first.get("label") or "검증 기믹 실패")
    incident, relation, action = (str(v) for v in summary)

    if ft is None:
        # Absence of a verified mechanic failure is not evidence of success.
        if "성공 판정 아님" not in incident:
            incident = (incident or "첫 기믹 실패는 아직 확인되지 않았어.") + " · 성공 판정 아님"
        if kt is not None:
            relation = (f"{_clock(kt)} 공대 대량 사망 관측 · "
                        "선행 기믹 실패 및 직접 원인은 확인되지 않았어.")
        return incident, relation, action

    if kt is None:
        return incident, relation, action

    # The collapse clock is *not* the generic candidate (causal.sec) clock.
    if kt < ft - 0.12:
        incident = f"{_clock(kt)} 공대 붕괴 시작 → {_clock(ft)} {label} 확인"
        relation = ("기믹 실패는 붕괴 이후에 관측됐어. 그 실패가 앞선 붕괴를 "
                    "일으켰다고 말할 근거는 없어.")
        action = "붕괴 직전 상황을 먼저 복기 · 이후 확인된 기믹 실패도 별도로 교정"
        return incident, relation, action

    # Try 31 pattern: the generic death candidate precedes firstMistake,
    # whereas the actual collapse is later. It is NOT a 'collapse-first' case.
    if ct is not None and ct < ft - 1.0:
        relation = (f"{_clock(ct)} 같은 주문 다수 사망 후보 관측 · "
                    f"붕괴 시작 {_clock(kt)} · 직접 인과관계는 미확정이야.")
        return incident, relation, action

    # A verified earlier mechanic does not automatically become collapseDirect.
    # Highlight sizable time separations without saying the two are linked.
    if kt >= ft + 5.0:
        incident = f"{_clock(ft)} {label} 관측 → {_clock(kt)} 공대 붕괴 관측"
        count = collapse.get("count")
        count_note = f"붕괴 구간 {int(count)}명 사망 · " if isinstance(count, int) and count > 0 else ""
        relation = (f"두 사건 사이 약 {kt-ft:.1f}초 · {count_note}"
                    "붕괴 직결 근거 미확정 · 개인 책임도 단정할 수 없어.")
    return incident, relation, action
